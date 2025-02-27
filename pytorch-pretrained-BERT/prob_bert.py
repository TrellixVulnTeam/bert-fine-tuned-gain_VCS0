# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import, division, print_function

import argparse
import csv
import logging
import os
import time
import random
import sys
import math
import collections

import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from torch.nn import CrossEntropyLoss, MSELoss
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import matthews_corrcoef, f1_score

from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE, WEIGHTS_NAME, CONFIG_NAME
from pytorch_pretrained_bert.modeling import BertForSequenceClassification,\
    BertConfig, BertForSequenceClassificationWithNoCls, BertForMsmarco
from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.optimization import BertAdam, warmup_linear

logger = logging.getLogger(__name__)

qrels = {}
with open("/data/users/maxinyu/pytorch-pretrained-BERT/data/glue/msmarco/qrels.dev.small.tsv",
          mode='r', encoding="utf-8") as f:
    reader = csv.reader(f, delimiter='\t')
    for row in reader:
        qid = int(row[0])
        did = int(row[2])
        if qid not in qrels:
            qrels[qid] = []
        qrels[qid].append(did)


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.

        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id


class DataProcessor(object):
    """Base class for data converters for sequence classification data sets."""

    def get_train_examples(self, data_dir):
        """Gets a collection of `InputExample`s for the train set."""
        raise NotImplementedError()

    def get_dev_examples(self, data_dir, output_dir=None):
        """Gets a collection of `InputExample`s for the dev set."""
        raise NotImplementedError()

    def get_labels(self):
        """Gets the list of labels for this data set."""
        raise NotImplementedError()

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        with open(input_file, "r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
            lines = []
            for line in reader:
                if sys.version_info[0] == 2:
                    line = list(unicode(cell, 'utf-8') for cell in line)
                lines.append(line)
            return lines


class QqpProcessor(DataProcessor):
    """Processor for the STS-B data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train.tsv")), "train")

    def get_dev_examples(self, data_dir, output_dir=None):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "dev.tsv")), "dev")

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        label_list = self.get_labels()
        for (i, line) in enumerate(lines):
            # if i > 100:
            #     break
            if i == 0:
                continue
            guid = "%s-%s" % (set_type, line[0])
            try:
                text_a = line[3]
                text_b = line[4]
                label = line[5]
                if label not in label_list:
                    continue
            except IndexError:
                continue
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class QnliProcessor(DataProcessor):
    """Processor for the STS-B data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train.tsv")), "train")

    def get_dev_examples(self, data_dir, output_dir=None):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "dev.tsv")),
            "dev_matched")

    def get_labels(self):
        """See base class."""
        return ["entailment", "not_entailment"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            # if i > 100:
            #     break
            guid = "%s-%s" % (set_type, line[0])
            text_a = line[1]
            text_b = line[2]
            label = line[-1]
            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class SnliProcessor(DataProcessor):
    """Processor for the SNLI data set (GLUE version)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "snli_1.0_train.txt")), "train")

    def get_dev_examples(self, data_dir, output_dir=None):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "snli_1.0_dev.txt")),
            "dev_matched")

    def get_labels(self):
        """See base class."""
        return ["neural", "entailment", "contradiction"]

    def _create_examples(self, lines, set_type):
        """Creates examples for the training and dev sets."""
        examples = []
        # Translation tables to remove parentheses and punctuation from
        # strings.
        parentheses_table = str.maketrans({'(': None, ')': None})
        for (i, line) in enumerate(lines):
            if i == 0:
                continue
            if line[0] == '-':
                continue
            pair_id = line[7]
            premise = line[1]
            hypothesis = line[2]
            # Remove '(' and ')' from the premises and hypotheses.
            premise = premise.translate(parentheses_table)
            premise = ' '.join([w for w in premise.rstrip().split()])
            hypothesis = hypothesis.translate(parentheses_table)
            hypothesis = ' '.join([w for w in hypothesis.rstrip().split()])
            id = pair_id
            guid = "%s-%s" % (set_type, id)
            text_a = premise
            text_b = hypothesis
            label = line[0]

            examples.append(
                InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
        return examples


class MsmarcoProcessor(DataProcessor):
    """Processor for the MsMarco data set (sample set)."""

    def get_train_examples(self, data_dir):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "train10000_sample.tsv")), "train")

    def get_dev_examples(self, data_dir, output_dir=None):
        """See base class."""
        return self._create_examples(
            self._read_tsv(os.path.join(data_dir, "dev1000_sample.tsv")), "dev", output_dir)

    def get_labels(self):
        """See base class."""
        return ["0", "1"]

    @classmethod
    def _read_tsv(cls, input_file, quotechar=None):
        """Reads a tab separated value file."""
        lines = []
        with open(input_file, 'r') as f:
            for i, line in enumerate(f):
                # if i > 1:
                #     break
                lines.append(line)
            return lines

    def _create_examples(self, lines, set_type, output_dir=None):
        """Creates examples for the training and dev sets."""
        examples = []
        queries_docs = collections.defaultdict(list)
        query_ids = {}
        for (i, line) in enumerate(lines):
            qid, query, docid, doc, label = line.rstrip().split('\t')
            if set_type == 'dev':
                queries_docs[query].append((docid, doc, label))
                query_ids[query] = qid
            else:
                guid = "%s-%s" % (set_type, line[0])
                text_a = query
                text_b = doc
                label = label
                examples.append(
                    InputExample(guid=guid, text_a=text_a, text_b=text_b, label=str(label)))
        if set_type == 'dev':
            # Add fake paragraphs to the queries that have less than FLAGS.num_eval_docs.
            queries = list(queries_docs.keys())  # Need to copy keys before iterating.
            for query in queries:
                docs = queries_docs[query]
                docs += max(
                    0, 1000 - len(docs)) * [('00000000', 'FAKE DOCUMENT', 0)]
                queries_docs[query] = docs

            assert len(
                set(len(docs) == 1000 for docs in queries_docs.values())) == 1, (
                'Not all queries have {} docs'.format(1000))

            with open(os.path.join(output_dir, "query_doc_ids_" + set_type + ".txt"), 'w') as ids_file:
                for i, (query, doc_ids_docs) in enumerate(queries_docs.items()):
                    doc_ids, docs, labels = zip(*doc_ids_docs)
                    query_id = query_ids[query]
                    guid = "%s-%s" % (set_type, query_id)
                    for i, (doc_text, label) in enumerate(zip(docs, labels)):
                        ids_file.write('\t'.join([query_id, doc_ids[i]]) + '\n')
                        # qid_docid.append((query_id, doc_ids[i]))
                        examples.append(
                            InputExample(guid=guid, text_a=query, text_b=doc_text, label=str(label)))
        return examples


def convert_examples_to_features(examples, label_list, max_seq_length,
                                 tokenizer, output_mode, mask_cls=True):
    """Loads a data file into a list of `InputBatch`s."""
    label_map = {label : i for i, label in enumerate(label_list)}

    features = []
    for (ex_index, example) in enumerate(examples):
        if ex_index % 10000 == 0:
            logger.info("Writing example %d of %d" % (ex_index, len(examples)))

        tokens_a = tokenizer.tokenize(example.text_a)

        tokens_b = None
        if example.text_b:
            tokens_b = tokenizer.tokenize(example.text_b)
            # Modifies `tokens_a` and `tokens_b` in place so that the total
            # length is less than the specified length.
            # Account for [CLS], [SEP], [SEP] with "- 3"
            _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
        else:
            # Account for [CLS] and [SEP] with "- 2"
            if len(tokens_a) > max_seq_length - 2:
                tokens_a = tokens_a[:(max_seq_length - 2)]

        # The convention in BERT is:
        # (a) For sequence pairs:
        #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
        #  type_ids: 0   0  0    0    0     0       0 0    1  1  1  1   1 1
        # (b) For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids: 0   0   0   0  0     0 0
        #
        # Where "type_ids" are used to indicate whether this is the first
        # sequence or the second sequence. The embedding vectors for `type=0` and
        # `type=1` were learned during pre-training and are added to the wordpiece
        # embedding vector (and position vector). This is not *strictly* necessary
        # since the [SEP] token unambiguously separates the sequences, but it makes
        # it easier for the model to learn the concept of sequences.
        #
        # For classification tasks, the first vector (corresponding to [CLS]) is
        # used as as the "sentence vector". Note that this only makes sense because
        # the entire model is fine-tuned.
        tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
        segment_ids = [0] * len(tokens)

        if tokens_b:
            tokens += tokens_b + ["[SEP]"]
            segment_ids += [1] * (len(tokens_b) + 1)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        if mask_cls:
            input_mask = [0]
        else:
            input_mask = [1]
        input_mask += [1] * (len(input_ids)-1)

        # Zero-pad up to the sequence length.
        while len(input_ids) != max_seq_length:
            input_ids.append(0)
            input_mask.append(0)
            segment_ids.append(0)

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        if output_mode == "classification":
            label_id = label_map[example.label]
        elif output_mode == "regression":
            label_id = float(example.label)
        else:
            raise KeyError(output_mode)

        if ex_index < 5:
            logger.info("*** Example ***")
            logger.info("guid: %s" % (example.guid))
            logger.info("tokens: %s" % " ".join(
                    [str(x) for x in tokens]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info(
                    "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info("label: %s (id = %d)" % (example.label, label_id))

        features.append(
                InputFeatures(input_ids=input_ids,
                              input_mask=input_mask,
                              segment_ids=segment_ids,
                              label_id=label_id))
    return features


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()


def simple_accuracy(preds, labels):
    return (preds == labels).mean()


def acc_and_f1(preds, labels):
    acc = simple_accuracy(preds, labels)
    f1 = f1_score(y_true=labels, y_pred=preds)
    return {
        "acc": acc,
        "f1": f1,
        "acc_and_f1": (acc + f1) / 2,
    }


def pearson_and_spearman(preds, labels):
    pearson_corr = pearsonr(preds, labels)[0]
    spearman_corr = spearmanr(preds, labels)[0]
    return {
        "pearson": pearson_corr,
        "spearmanr": spearman_corr,
        "corr": (pearson_corr + spearman_corr) / 2,
    }


def mrr10(preds, output_dir=None):
    qid_docid = []
    with open(os.path.join(output_dir, "query_doc_ids_dev.txt")) as ref_file:
        for line in ref_file:
            qid_docid.append(line.strip().split("\t"))
    assert len(qid_docid) > 0
    total_count = 0
    res_dev = {}
    start_time = time.time()
    for item in preds:
        if total_count % 10000 == 0:
            logger.info("Read {} examples in {} secs".format(
                total_count, int(time.time() - start_time)))
        query_id = qid_docid[total_count][0]
        doc_id = qid_docid[total_count][1]
        if doc_id != "00000000":
            if int(query_id) not in res_dev:
                res_dev[int(query_id)] = {}
            if int(doc_id) not in res_dev[int(query_id)]:
                res_dev[int(query_id)][int(doc_id)] = 0
            res_dev[int(query_id)][int(doc_id)] = item[1]
        total_count += 1
    mrr = 0
    for qid, docs in res_dev.items():
        ranked = sorted(docs, key=docs.get, reverse=True)
        for i in range(min(len(ranked), 10)):
            if ranked[i] in qrels[qid]:
                mrr += 1 / (i + 1)
                break
    mrr /= len(res_dev)
    return mrr


def compute_metrics(task_name, preds, labels, output_dir=None):
    assert len(preds) == len(labels)
    if task_name == "cola":
        return {"mcc": matthews_corrcoef(labels, preds)}
    elif task_name == "sst-2":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "mrpc":
        return acc_and_f1(preds, labels)
    elif task_name == "sts-b":
        return pearson_and_spearman(preds, labels)
    elif task_name == "qqp":
        return acc_and_f1(preds, labels)
    elif task_name == "mnli":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "mnli-mm":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "qnli":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "rte":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "wnli":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "msmarco":
        return {"mrr": mrr10(preds, output_dir)}
    else:
        raise KeyError(task_name)


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                        "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                        "bert-base-multilingual-cased, bert-base-chinese.")
    parser.add_argument("--task_name",
                        default=None,
                        type=str,
                        required=True,
                        help="The name of the task to train.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--cache_dir",
                        default="",
                        type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length",
                        default=128,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=1,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--layer_index_to_prob",
                        default=12,
                        type=int,
                        help="which layer to use to get the output")
    parser.add_argument("--mask_cls",
                        action='store_true',
                        help="Whether or not to use the [CLS] vector")
    parser.add_argument("--freeze_bert",
                        action='store_true',
                        help="Whether not to freeze the bert weights when fine-tune")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--patience",
                        default=3.0,
                        type=float,
                        help="Break after these epochs when metric does not improve.")
    parser.add_argument("--threshold",
                        default=0.005,
                        type=float,
                        help="threshold for restricted improvements.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")
    parser.add_argument('--server_ip', type=str, default='', help="Can be used for distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="Can be used for distant debugging.")
    args = parser.parse_args()
    if not os.path.exists(args.output_dir):
        os.mkdir(args.output_dir)

    processors = {
        "qqp": QqpProcessor,
        "qnli": QnliProcessor,
        "msmarco": MsmarcoProcessor
    }

    output_modes = {
        "qqp": "classification",
        "qnli": "classification",
        "msmarco": "classification"
    }

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)

    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train:
        raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    task_name = args.task_name.lower()

    if task_name not in processors:
        raise ValueError("Task not found: %s" % (task_name))

    processor = processors[task_name]()
    output_mode = output_modes[task_name]

    label_list = processor.get_labels()
    num_labels = len(label_list)

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_optimization_steps = None
    if args.do_train:
        train_examples = processor.get_train_examples(args.data_dir)
        num_train_optimization_steps = int(
            math.ceil(len(train_examples)) / args.train_batch_size / args.gradient_accumulation_steps) * args.num_train_epochs
        if args.local_rank != -1:
            num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()
    logger.info("num_train_optimization_steps:{}".format(num_train_optimization_steps))

    # Prepare model
    cache_dir = args.cache_dir if args.cache_dir else os.path.join(str(PYTORCH_PRETRAINED_BERT_CACHE),
                                                                   'distributed_{}'.format(args.local_rank))
    model = BertForSequenceClassificationWithNoCls.from_pretrained(args.bert_model,
                                                          cache_dir=cache_dir,
                                                          num_labels=num_labels)
    # model = BertForMsmarco.from_pretrained(args.bert_model,
    #                                        cache_dir=cache_dir,
    #                                        num_labels=num_labels)
    model.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        model = DDP(model)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    if args.freeze_bert:
        param_optimizer = model.named_parameters()
        freeze = ['bert']
        for n, param in param_optimizer:
            if any(name in n for name in freeze):
                param.requires_grad = False

    # Prepare optimizer
    if args.do_train:
        list_param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in list_param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in list_param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=num_train_optimization_steps)

    best_epoch = 0
    best_metric = 0
    global_step = 0
    if args.do_train:
        # Train data loader
        train_features = convert_examples_to_features(
            train_examples, label_list, args.max_seq_length, tokenizer, output_mode, args.mask_cls)
        all_train_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_train_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        all_train_segment_ids = torch.tensor([f.segment_ids for f in train_features], dtype=torch.long)

        if output_mode == "classification":
            all_train_label_ids = torch.tensor([f.label_id for f in train_features], dtype=torch.long)
        elif output_mode == "regression":
            all_train_label_ids = torch.tensor([f.label_id for f in train_features], dtype=torch.float)

        train_data = TensorDataset(all_train_input_ids, all_train_input_mask,
                                   all_train_segment_ids, all_train_label_ids)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        # Eval data loader
        eval_examples = processor.get_dev_examples(args.data_dir, args.output_dir)
        eval_features = convert_examples_to_features(
            eval_examples, label_list, args.max_seq_length, tokenizer, output_mode, args.mask_cls)
        all_eval_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        all_eval_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        all_eval_segment_ids = torch.tensor([f.segment_ids for f in eval_features], dtype=torch.long)

        if output_mode == "classification":
            all_eval_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.long)
        elif output_mode == "regression":
            all_eval_label_ids = torch.tensor([f.label_id for f in eval_features], dtype=torch.float)

        eval_data = TensorDataset(all_eval_input_ids, all_eval_input_mask,
                                  all_eval_segment_ids, all_eval_label_ids)
        # Run prediction for full data
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)
        for epoch in trange(int(args.num_train_epochs), desc="Epoch"):
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            model.train()
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids = batch

                # define a new function to compute loss values for both output_modes
                logits = model(input_ids, segment_ids, input_mask, labels=None,
                               layer_index=args.layer_index_to_prob)

                if output_mode == "classification":
                    loss_fct = CrossEntropyLoss()
                    loss = loss_fct(logits.view(-1, num_labels), label_ids.view(-1))
                elif output_mode == "regression":
                    loss_fct = MSELoss()
                    loss = loss_fct(logits.view(-1), label_ids.view(-1))

                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()
                # if (step+1) % 10 == 0:
                #     logger.info("step: {}, batch loss: {}".format(step, loss))
                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    if args.fp16:
                        # modify learning rate with special warm up BERT uses
                        # if args.fp16 is False, BertAdam is used that handles this automatically
                        lr_this_step = args.learning_rate * warmup_linear(global_step/num_train_optimization_steps,
                                                                          args.warmup_proportion)
                        # lr_this_step = args.learning_rate * warmup_linear.get_lr(global_step, args.warmup_proportion)
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = lr_this_step
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1
            # dev
            logger.info("***** Running evaluation *****")
            logger.info("  Num examples = %d", len(eval_examples))
            logger.info("  Batch size = %d", args.eval_batch_size)
            model.eval()
            preds = []
            for input_ids, input_mask, segment_ids, label_ids in tqdm(eval_dataloader, desc="Evaluating"):
                input_ids = input_ids.to(device)
                input_mask = input_mask.to(device)
                segment_ids = segment_ids.to(device)

                with torch.no_grad():
                    logits = model(input_ids, segment_ids, input_mask, labels=None,
                                   layer_index=args.layer_index_to_prob)
                if len(preds) == 0:
                    preds.append(logits.detach().cpu().numpy())
                else:
                    preds[0] = np.append(
                        preds[0], logits.detach().cpu().numpy(), axis=0)

            preds = preds[0]
            if output_mode == "classification":
                # msmarco needs probability
                if args.task_name != 'msmarco':
                    preds = np.argmax(preds, axis=1)
            elif output_mode == "regression":
                preds = np.squeeze(preds)
            result = compute_metrics(task_name, preds, all_eval_label_ids.numpy(), args.output_dir)
            if task_name != "msmarco":
                metric = result['acc']
            else:
                metric = result["mrr"]
            logger.info("Epoch: {}, metric: {}".format(epoch, metric))
            if metric >= best_metric + args.threshold: # threshold for metric improvement
                best_metric = metric
                best_epoch = epoch
                # Save a trained model, configuration and tokenizer
                model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self

                # If we save using the predefined names, we can load using `from_pretrained`
                output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
                output_config_file = os.path.join(args.output_dir, CONFIG_NAME)

                torch.save(model_to_save.state_dict(), output_model_file)
                model_to_save.config.to_json_file(output_config_file)
                tokenizer.save_vocabulary(args.output_dir)
            else:
                if metric > best_metric:
                    best_metric = metric
                    best_epoch = epoch
                    # Save a trained model, configuration and tokenizer
                    model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self

                    # If we save using the predefined names, we can load using `from_pretrained`
                    output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
                    output_config_file = os.path.join(args.output_dir, CONFIG_NAME)

                    torch.save(model_to_save.state_dict(), output_model_file)
                    model_to_save.config.to_json_file(output_config_file)
                    tokenizer.save_vocabulary(args.output_dir)
                if epoch >= best_epoch + args.patience:
                    break
        logger.info("Best Epoch: {}, Metric: {}".format(best_epoch, best_metric))
        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        with open(output_eval_file, "w") as writer:
            writer.write("best epoch: %s \nacc: %s\n" % (best_epoch, best_metric))


if __name__ == "__main__":
    main()
