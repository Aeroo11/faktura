"""Donut's JSONParseEvaluator: nTED-based accuracy and field-level F1.

Ported from https://github.com/clovaai/donut (donut/util.py) so CORD scores are directly
comparable with the published Donut numbers. The logic is unchanged. The only difference
is that nltk.edit_distance (defaults: substitution cost 1, no transpositions) is replaced
by an equivalent plain Levenshtein function, to avoid the nltk dependency.

Original notice:

    Donut
    Copyright (c) 2022-present NAVER Corp.
    MIT License

    Permission is hereby granted, free of charge, to any person obtaining a copy of this
    software and associated documentation files (the "Software"), to deal in the Software
    without restriction, including without limitation the rights to use, copy, modify,
    merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to the following
    conditions:

    The above copyright notice and this permission notice shall be included in all copies
    or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
    INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
    PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
    HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
    CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
    OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

from typing import Any

import zss
from zss import Node


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance; equals nltk.edit_distance(a, b) with default arguments."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class JSONParseEvaluator:
    """Calculate n-TED (normalized tree edit distance) based accuracy and F1 score."""

    @staticmethod
    def flatten(data: dict) -> list[tuple[str, Any]]:
        flatten_data = []

        def _flatten(value: Any, key: str = "") -> None:
            if type(value) is dict:
                for child_key, child_value in value.items():
                    _flatten(child_value, f"{key}.{child_key}" if key else child_key)
            elif type(value) is list:
                for value_item in value:
                    _flatten(value_item, key)
            else:
                flatten_data.append((key, value))

        _flatten(data)
        return flatten_data

    @staticmethod
    def update_cost(node1: Node, node2: Node) -> int:
        label1 = node1.label
        label2 = node2.label
        label1_leaf = "<leaf>" in label1
        label2_leaf = "<leaf>" in label2
        if label1_leaf and label2_leaf:
            return edit_distance(label1.replace("<leaf>", ""), label2.replace("<leaf>", ""))
        if not label1_leaf and label2_leaf:
            return 1 + len(label2.replace("<leaf>", ""))
        if label1_leaf and not label2_leaf:
            return 1 + len(label1.replace("<leaf>", ""))
        return int(label1 != label2)

    @staticmethod
    def insert_and_remove_cost(node: Node) -> int:
        label = node.label
        if "<leaf>" in label:
            return len(label.replace("<leaf>", ""))
        return 1

    def normalize_dict(self, data: dict | list | Any) -> Any:
        """Sort keys by (length, name); iterate over lists; drop empty values."""
        if not data:
            return {}

        if isinstance(data, dict):
            new_data: Any = {}
            for key in sorted(data.keys(), key=lambda k: (len(k), k)):
                value = self.normalize_dict(data[key])
                if value:
                    if not isinstance(value, list):
                        value = [value]
                    new_data[key] = value
        elif isinstance(data, list):
            if all(isinstance(item, dict) for item in data):
                new_data = []
                for item in data:
                    item = self.normalize_dict(item)
                    if item:
                        new_data.append(item)
            else:
                new_data = [
                    str(item).strip()
                    for item in data
                    if type(item) in {str, int, float} and str(item).strip()
                ]
        else:
            new_data = [str(data).strip()]

        return new_data

    def cal_f1(self, preds: list[dict], answers: list[dict]) -> float:
        """Global field-level F1, micro-averaged over all samples."""
        total_tp, total_fn_or_fp = 0, 0
        for pred, answer in zip(preds, answers, strict=True):
            pred_f = self.flatten(self.normalize_dict(pred))
            answer_f = self.flatten(self.normalize_dict(answer))
            for field in pred_f:
                if field in answer_f:
                    total_tp += 1
                    answer_f.remove(field)
                else:
                    total_fn_or_fp += 1
            total_fn_or_fp += len(answer_f)
        return total_tp / (total_tp + total_fn_or_fp / 2)

    def construct_tree_from_dict(self, data: dict | list, node_name: str | None = None) -> Node:
        if node_name is None:
            node_name = "<root>"
        node = Node(node_name)
        if isinstance(data, dict):
            for key, value in data.items():
                node.addkid(self.construct_tree_from_dict(value, key))
        elif isinstance(data, list):
            if all(isinstance(item, dict) for item in data):
                for item in data:
                    node.addkid(self.construct_tree_from_dict(item, "<subtree>"))
            else:
                for item in data:
                    node.addkid(Node(f"<leaf>{item}"))
        else:
            raise Exception(data, node_name)
        return node

    def _distance(self, a: Node, b: Node) -> float:
        return zss.distance(
            a,
            b,
            get_children=zss.Node.get_children,
            insert_cost=self.insert_and_remove_cost,
            remove_cost=self.insert_and_remove_cost,
            update_cost=self.update_cost,
            return_operations=False,
        )

    def cal_acc(self, pred: dict, answer: dict) -> float:
        """max(0, 1 - TED(pred, answer) / TED(empty, answer))."""
        pred_t = self.construct_tree_from_dict(self.normalize_dict(pred))
        answer_t = self.construct_tree_from_dict(self.normalize_dict(answer))
        empty_t = self.construct_tree_from_dict(self.normalize_dict({}))
        return max(0, 1 - self._distance(pred_t, answer_t) / self._distance(empty_t, answer_t))
