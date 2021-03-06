# coding=utf-8
# minimal-rnr-qa
# Copyright 2021-present NAVER Corp.
# Apache License v2.0

import argparse
from distutils.util import strtobool

from minimal_rnr.utils.logger import get_logger
from minimal_rnr.pytorch.inferencer import TorchMinimalRnR


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, required=True)

    parser.add_argument("--model_path", default="/models")  # what model?
    parser.add_argument("--resources_path", default="/resources")  # what resource?
    parser.add_argument("--use_faiss_index", default=True, type=strtobool)

    parser.add_argument("--top_k", type=int, default=50)
    parser.add_argument("--passage_score_weight", type=float, default=None)

    parser.add_argument("--input_path", type=str, default=None, required=True)  # what?
    parser.add_argument("--output_path", type=str, default="predictions.jsonl", required=True)

    args = parser.parse_args()
    return args


def main(args):
    logger = get_logger("minimal-rnr-qa")
    logger.info(vars(args))

    minimal_rnr = TorchMinimalRnR(args)
    minimal_rnr.inference_on_file(args.input_path, args.output_path, args.top_k, args.passage_score_weight)


if __name__ == "__main__":
    args = get_args()
    main(args)
