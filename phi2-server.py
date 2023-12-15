import argparse
from typing import Optional
from dataclasses import dataclass
from mlx.utils import tree_unflatten
from transformers import AutoTokenizer

import mlx.core as mx
import mlx.nn as nn
import math

from flask import Flask, request, jsonify
import time

app = Flask(__name__)
default_token_max = 512

@dataclass
class ModelArgs:
    max_sequence_length: int = 2048
    num_vocab: int = 51200
    model_dim: int = 2560
    num_heads: int = 32
    num_layers: int = 32
    rotary_dim: int = 32


class LayerNorm(nn.LayerNorm):
    def __call__(self, x: mx.array) -> mx.array:
        return super().__call__(x.astype(mx.float32)).astype(x.dtype)


class RoPEAttention(nn.Module):
    def __init__(self, dims: int, num_heads: int, rotary_dim: int):
        super().__init__()

        self.num_heads = num_heads

        self.rope = nn.RoPE(rotary_dim, traditional=False)
        self.Wqkv = nn.Linear(dims, 3 * dims)
        self.out_proj = nn.Linear(dims, dims)

    def __call__(self, x, mask=None, cache=None):
        qkv = self.Wqkv(x)
        queries, keys, values = mx.split(qkv, 3, axis=-1)

        # Extract some shapes
        num_heads = self.num_heads
        B, L, D = queries.shape

        # Prepare the queries, keys and values for the attention computation
        queries = queries.reshape(B, L, num_heads, -1).transpose(0, 2, 1, 3)
        keys = keys.reshape(B, L, num_heads, -1).transpose(0, 2, 1, 3)
        values = values.reshape(B, L, num_heads, -1).transpose(0, 2, 1, 3)

        # Add RoPE to the queries and keys and combine them with the cache
        if cache is not None:
            key_cache, value_cache = cache
            queries = self.rope(queries, offset=key_cache.shape[2])
            keys = self.rope(keys, offset=key_cache.shape[2])
            keys = mx.concatenate([key_cache, keys], axis=2)
            values = mx.concatenate([value_cache, values], axis=2)
        else:
            queries = self.rope(queries)
            keys = self.rope(keys)

        queries = queries.astype(mx.float32)
        keys = keys.astype(mx.float32)

        # Finally perform the attention computation
        scale = math.sqrt(1 / queries.shape[-1])
        scores = (queries * scale) @ keys.transpose(0, 1, 3, 2)
        if mask is not None:
            scores = scores + mask

        scores = mx.softmax(scores, axis=-1).astype(values.dtype)
        values_hat = (scores @ values).transpose(0, 2, 1, 3).reshape(B, L, -1)

        return self.out_proj(values_hat), (keys, values)


class ParallelBlock(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        dims = config.model_dim
        mlp_dims = dims * 4
        self.mixer = RoPEAttention(dims, config.num_heads, config.rotary_dim)
        self.ln = LayerNorm(dims)
        self.fc1 = nn.Linear(dims, mlp_dims)
        self.fc2 = nn.Linear(mlp_dims, dims)
        self.act = nn.GELU(approx="precise")

    def __call__(self, x, mask, cache):
        h = self.ln(x)
        attn_h, cache = self.mixer(h, mask, cache)
        ff_h = self.fc2(self.act(self.fc1(h)))
        return attn_h + ff_h + x, cache


class TransformerDecoder(nn.Module):
    def __init__(self, config: ModelArgs):
        super().__init__()
        self.h = [ParallelBlock(config) for i in range(config.num_layers)]

    def __call__(self, x, mask, cache):
        if cache is None:
            cache = [None] * len(self.h)

        for e, layer in enumerate(self.h):
            x, cache[e] = layer(x, mask, cache[e])
        return x, cache


class OutputHead(nn.Module):
    def __init__(self, config: ModelArgs) -> None:
        self.ln = LayerNorm(config.model_dim)
        self.linear = nn.Linear(config.model_dim, config.num_vocab)

    def __call__(self, inputs):
        return self.linear(self.ln(inputs))


class Phi2(nn.Module):
    def __init__(self, config: ModelArgs):
        self.wte = nn.Embedding(config.num_vocab, config.model_dim)
        self.transformer = TransformerDecoder(config)
        self.lm_head = OutputHead(config)

    def __call__(
        self,
        inputs: mx.array,
        mask: mx.array = None,
        cache: mx.array = None,
    ) -> tuple[mx.array, mx.array]:
        x = self.wte(inputs)

        mask = None
        if x.shape[1] > 1:
            mask = nn.MultiHeadAttention.create_additive_causal_mask(x.shape[1])
            mask = mask.astype(x.dtype)

        y, cache = self.transformer(x, mask, cache)
        return self.lm_head(y), cache


def generate(prompt: mx.array, model: Phi2, temp: Optional[float] = 0.0):
    def sample(logits):
        if temp == 0:
            return mx.argmax(logits, axis=-1)
        else:
            return mx.random.categorical(logits * (1 / temp))

    logits, cache = model(prompt)
    y = sample(logits[:, -1, :])
    yield y

    while True:
        logits, cache = model(y[:, None], cache=cache)
        y = sample(logits.squeeze(1))
        yield y


def load_model():
    model = Phi2(ModelArgs())
    weights = mx.load("weights.npz")
    model.update(tree_unflatten(list(weights.items())))
    tokenizer = AutoTokenizer.from_pretrained("microsoft/phi-2", trust_remote_code=True)
    return model, tokenizer

def complete_text(str_prompt, temp, max_tokens):
    prompt = tokenizer(
        str_prompt,
        return_tensors="np",
        return_attention_mask=False,
    )["input_ids"]
    tk_prompt = len(prompt[0])
    tk_gen = 0

    prompt = mx.array(prompt)

    #print("[INFO] Generating with Phi-2...", flush=True)
    #print(prompt_fmt, end="", flush=True)
    ret = ''
    eos = False

    tokens = []
    for token, _ in zip(generate(prompt, model, temp), range(max_tokens)):
        tokens.append(token)

        if (len(tokens) % 10) == 0:
            mx.eval(tokens)
            s = tokenizer.decode([t.item() for t in tokens])
            tk_gen +=len(tokens)
            sfiltered = s
            tokens = []
            if s.find('<|endoftext|>') >=0:
                sfiltered = s.split('<|endoftext|>',1)[0]
                eos = True
                break
            #print(sfiltered, end="", flush=True)
            ret += sfiltered

    if not eos:
        mx.eval(tokens)
        s = tokenizer.decode([t.item() for t in tokens])
        sfiltered = s
        tk_gen +=len(tokens)
        tokens = []
        if s.find('<|endoftext|>') >=0:
            sfiltered = s.split('<|endoftext|>',1)[0]
        ret += sfiltered
        #print(sfiltered, flush=True)
    return ret, (tk_prompt, tk_gen)

@app.route('/v1/chat/completions', methods=['POST'])
def api_completions():
    # Get JSON data sent with the request
    data = request.get_json()
    print(data)
    model_name = 'phi-2'
    ai_role = 'Assistance'
    prompt_strs = "%s: %s\n%s: " % (
        data['messages'][0]['role'],
        data['messages'][0]['content'],
        'Assistance')
    temp = 0.7
    if 'temperature' in data:
        temp = data['temperature']
    txt, tks = complete_text(prompt_strs, temp, default_token_max)
    txt = txt.strip()
    
    resp = {
      "id": "gen-resp-1",
      "object": "chat.completion",
      "created": int(time.time()),
      "model": model_name,
      "usage": {
          "prompt_tokens": tks[0],
          "completion_tokens": tks[1],
          "total_tokens": tks[0] + tks[1]
      },
      "choices": [
          {
              "message": {
                  "role": ai_role,
                  "content": txt
              },
              "finish_reason": "stop",
              "index": 0
          }
      ]
    }

    return jsonify(resp)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Phi-2 inference script")
    parser.add_argument(
        "--prompt",
        help="The message to be processed by the model",
        default="Write a detailed analogy between mathematics and a lighthouse.",
    )
    parser.add_argument(
        "--max_tokens",
        "-m",
        type=int,
        default=1024,
        help="Maximum number of tokens to generate",
    )
    parser.add_argument(
        "--temp",
        help="The sampling temperature.",
        type=float,
        default=0.0,
    )
    parser.add_argument("--seed", type=int, default=0, help="The PRNG seed")
    args = parser.parse_args()
    default_token_max = args.max_tokens

    mx.random.seed(args.seed)

    model, tokenizer = load_model()
    
    #for i in range(5):
    #    gen_text(args.prompt, float(i)/5)
    app.run(debug=True)
