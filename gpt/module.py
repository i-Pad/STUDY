''' Based on Attention Is All You Need '''
'''  https://arxiv.org/abs/1706.03762  '''

import torch
import torch.nn as nn
import math
import copy
import pandas as pd
import altair as alt
import torchtext.datasets as datasets
import spacy
import GPUtil
import warnings
import torch.distributed as dist
import torch.multiprocessing as mp

from os.path import exists
from torch.nn.functional import log_softmax, pad
from torch.optim.lr_scheduler import LambdaLR
from torchtext.data.functional import to_map_style_dataset
from torch.utils.data import DataLoader
from torchtext.vocab import build_vocab_from_iterator
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP

warnings.filterwarnings('ignore')

class EncoderDecoder(nn.Module):
	def __init__(self, encoder, decoder, src_embed, tgt_embed, generator):
		super(EncoderDecoder, self).__init__()
		self.encoder = encoder
		self.decoder = decoder
		self.src_embed = src_embed
		self.tgt_embed = tgt_embed
		self.generator = generator

	def forward(self, src, tgt, src_mask, tgt_mask):
		return self.decode(self.encode(src, src_mask), src_mask, tgt, tgt_mask)

	def encode(self, src, src_mask):
		return self.encoder(self.src_embed(src), src_mask)

	def decode(self, memory, src_mask, tgt, tgt_mask):
		return self.decoder(self.tgt_embed(tgt), memory, src_mask, tgt_mask)

class Generator(nn.Module):
	def __init__(self, d_model, vocab):
		super(Generator, self).__init__()
		self.proj = nn.Linear(d_model, vocab)

	def forward(self, x):
		return log_softmax(self.proj(x), dim=-1)

def stacks(module, N):
	return nn.ModuleList([copy.deepcopy(module) for _ in range(N)])

class Encoder(nn.Module):
	def __init__(self, layer, N):
		super(Encoder, self).__init__()
		self.layers = stacks(layer, N)
		self.norm = LayerNorm(layer.size)

	def forward(self, x, mask):
		for layer in self.layers:
			x = layer(x, mask)

		return self.norm(x)

class LayerNorm(nn.Module):
	def __init__(self, features, eps=1e-6):
		super(LayerNorm, self).__init__()
		self.a_2 = nn.Parameter(torch.ones(features))
		self.b_2 = nn.Parameter(torch.zeros(features))
		self.eps = eps

	def forward(self, x):
		mean = x.mean(-1, keepdim=True)
		std = x.std(-1, keepdim=True)

		return self.a_2 * (x - mean) / (std + self.eps) + self.b_2

class SublayerConnection(nn.Module):
	def __init__(self, size, dropout):
		super(SublayerConnection, self).__init__()
		self.norm = LayerNorm(size)
		self.dropout = nn.Dropout(dropout)

	def forward(self, x, sublayer):
		return x + self.dropout(sublayer(self.norm(x)))

class EncoderLayer(nn.Module):
	def __init__(self, size, self_attn, feed_forward, dropout):
		super(EncoderLayer, self).__init__()
		self.self_attn = self_attn
		self.feed_forward = feed_forward
		self.sublayer = stacks(SublayerConnection(size, dropout), 2)
		self.size = size

	def forward(self, x, mask):
		x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, mask))

		return self.sublayer[1](x, self.feed_forward)

class Decoder(nn.Module):
	def __init__(self, layer, N):
		super(Decoder, self).__init__()
		self.layers = stacks(layer, N)
		self.norm = LayerNorm(layer.size)

	def forward(self, x, memory, src_mask, tgt_mask):
		for layer in self.layers:
			x = layer(x, memory, src_mask, tgt_mask)

		return self.norm(x)

class DecoderLayer(nn.Module):
	def __init__(self, size, self_attn, src_attn, feed_forward, dropout):
		super(DecoderLayer, self).__init__()
		self.size = size
		self.self_attn = self_attn
		self.src_attn = src_attn
		self.feed_forward = feed_forward
		self.sublayer = stacks(SublayerConnection(size, dropout), 3)

	def forward(self, x, memory, src_mask, tgt_mask):
		m = memory
		x = self.sublayer[0](x, lambda x: self.self_attn(x, x, x, tgt_mask))
		x = self.sublayer[1](x, lambda x: self.src_attn(x, m, m, src_mask))

		return self.sublayer[2](x, self.feed_forward)

def subsequent_mask(size):
	attn_shape = (1, size, size)
	subsequent_mask = torch.triu(torch.ones(attn_shape), diagonal=1).type(torch.uint8)

	return subsequent_mask == 0

def attention(query, key, value, mask=None, dropout=None):
	# query, key and value: (n_batches, seq_len, d_k)
	# mask: (n_batches, seq_len, seq_len)

	d_k = query.size(-1)
	scores = torch.matmul(query, key.transpose(-2, -1)) / math.sqrt(d_k) # Q x K^T / sqrt(d_k) -> (n_batches, seq_len, seq_len)

	if mask is not None:
		scores = scores.masked_fill(mask == 0, -1e9)

	p_attn = scores.softmax(dim=-1) # (n_batches, seq_len, seq_len)

	if dropout is not None:
		p_attn = dropout(p_attn)

	return torch.matmul(p_attn, value), p_attn # (n_batches, seq_len, d_k)

class MultiHeadedAttention(nn.Module):
	def __init__(self, h, d_model, dropout=0.1):
		super(MultiHeadedAttention, self).__init__()
		# assumed d_embed equals to d_model because typically it is.

		assert d_model % h == 0
		self.d_k = d_model // h
		self.h = h
		self.linears = stacks(nn.Linear(d_model, d_model), 4) # 3 for query, key and value for each and 1 for output
		self.attn = None
		self.dropout = nn.Dropout(p=dropout)

	def forward(self, query, key, value, mask=None):
		# query, key and value: (n_batches, seq_len, d_model)
		# mask: (n_batches, seq_len, seq_len)

		if mask is not None:
			mask = mask.unsqueeze(1)

		n_batches = query.size(0)

		qeury, key, value = [lin(x).view(n_batches, -1, self.h, self.d_k).transpose(1, 2) for lin, x in zip(self.linears, (query, key, value))]
		# history of above:
		# (n_batches, seq_len, d_model) (input)
		# (n_batches, seq_len, h, d_k)
		# (n_batches, h, seq_len, d_k)

		x, self.attn = attention(qeury, key, value, mask=mask, dropout=self.dropout)

		x = x.transpose(1, 2).contiguous().view(n_batches, -1, self.h * self.d_k)
		# history of above:
		# (n_batches, h, seq_len, d_k) (input)
		# (n_batches, seq_len, h, d_k)
		# (n_batches, seq_len, d_model)

		del query
		del key
		del value

		return self.linears[-1](x) # (n_batches, seq_len, d_model)

class PositionwiseFeedForward(nn.Module):
	def __init__(self, d_model, d_ff, dropout=0.1):
		super(PositionwiseFeedForward, self).__init__()
		self.w_1 = nn.Linear(d_model, d_ff)
		self.w_2 = nn.Linear(d_ff, d_model)
		self.dropout = nn.Dropout(dropout)

	def forward(self, x):
		return self.w_2(self.dropout(self.w_1(x).relu()))

class Embeddings(nn.Module):
	def __init__(self, d_model, vocab):
		super(Embeddings, self).__init__()
		self.lut = nn.Embedding(vocab, d_model)
		self.d_model = d_model

	def forward(self, x):
		return self.lut(x) * math.sqrt(self.d_model)

class PositionalEncoding(nn.Module):
	def __init__(self, d_model, dropout, max_len=5000):
		super(PositionalEncoding, self).__init__()
		self.dropout = nn.Dropout(p=dropout)

		pe = torch.zeros(max_len, d_model)
		position = torch.arange(0, max_len).unsqueeze(1)
		div_term = torch.exp(torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model))
		pe[:, 0::2] = torch.sin(position * div_term)
		pe[:, 1::2] = torch.cos(position * div_term)
		pe = pe.unsqueeze(0)
		self.register_buffer('pe', pe)#do not update

	def forward(self, x):
		x = x + self.pe[:, :x.size(1)].requires_grad_(False)

		return self.dropout(x)

def make_model(src_vocab, tgt_vocab, N=6, d_model=512, d_ff=2048, h=8, dropout=0.1):
	c = copy.deepcopy

	attn = MultiHeadedAttention(h, d_model)
	ff = PositionwiseFeedForward(d_model, d_ff, dropout)
	position = PositionalEncoding(d_model, dropout)

	model = EncoderDecoder(
		Encoder(EncoderLayer(d_model, c(attn), c(ff), dropout), N),
		Decoder(DecoderLayer(d_model, c(attn), c(attn), c(ff), dropout), N),
		nn.Sequential(Embeddings(d_model, src_vocab), c(position)),
		nn.Sequential(Embeddings(d_model, tgt_vocab), c(position)),
		Generator(d_model, tgt_vocab),
	)

	for p in model.parameters():
		if p.dim() > 1:
			nn.init.xavier_uniform_(p)

	return model

def main():
	print('import is done.')

	'''
	LS_data = pd.concat([pd.DataFrame({"Subsequent Mask": subsequent_mask(20)[0][x, y].flatten(),"Window": y,"Masking": x,}) for y in range(20) for x in range(20)])
	print(LS_data)

	alt.Chart(LS_data).mark_rect().properties(height=250, width=250).encode(alt.X("Window:O"),alt.Y("Masking:O"),alt.Color("Subsequent Mask:Q", scale=alt.Scale(scheme="viridis")),).interactive()
	'''

	'''
	pe = PositionalEncoding(20, 0)
	y = pe.forward(torch.zeros(1, 100, 20))

	data = pd.concat([pd.DataFrame({"embedding": y[0, :, dim], "dimension": dim, "position": list(range(100)),}) for dim in [4, 5, 6, 7]])
	print(data)

	alt.Chart(data).mark_line().properties(width=800).encode(x="position", y="embedding", color="dimension:N").interactive()
	'''

	test_model = make_model(11, 11, 2)
	test_model.eval()
	src = torch.LongTensor([[1, 2, 3, 4, 5, 6, 7, 8, 9 ,10]])
	src_mask = torch.ones(1, 1, 10)

	memory = test_model.encode(src, src_mask)
	ys = torch.zeros(1, 1).type_as(src)

	for _ in range(9):
		out = test_model.decode(memory, src_mask, ys, subsequent_mask(ys.size(1)).type_as(src.data))
		prob = test_model.generator(out[:, -1])
		_, next_word = torch.max(prob, dim=1)
		next_word = next_word.data[0]
		ys = torch.cat([ys, torch.empty(1, 1).type_as(src.data).fill_(next_word)], dim=1)

	print('Example Untrained Model Prediction:', ys)



if __name__ == '__main__':
	main()
