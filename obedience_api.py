"""Public Obedience API client and non-sensitive pathway data models.

This module deliberately contains no synthesis, mapping, or private solver
implementation. It is safe to distribute with the API-backed UI.
"""

from __future__ import annotations

import csv
import itertools
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from obedience_client import (
    ApiError,
    JobStatus,
    ObedienceApiClient,
    ObedienceApiError,
)

__all__ = [
    "ApiError",
    "F_STRING_PLACEHOLDER",
    "JobStatus",
    "LogicEdge",
    "LogicGraph",
    "LogicNode",
    "LogicPort",
    "ObedienceApiClient",
    "ObedienceApiError",
    "TruthTable",
]

F_STRING_PLACEHOLDER = re.compile(r"(?<!\{)\{([^{}]+)\}(?!\})")


@dataclass
class TruthTable:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str | None]] = field(default_factory=list)
    column_kinds: list[str] = field(default_factory=list)
    parameter_domains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    parameter_positions: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.column_kinds:
            self.column_kinds = [self._infer_kind(column) for column in self.columns]
        if len(self.column_kinds) != len(self.columns):
            raise ValueError("column_kinds must contain one kind for every column.")

    @staticmethod
    def _infer_kind(column: str) -> str:
        name = column.strip().lower()
        return "output" if name.startswith("out") or name in {"y", "z"} else "input"

    def sort_columns(self) -> None:
        order = sorted(
            range(len(self.columns)),
            key=lambda index: {"input": 0, "derived": 1, "output": 2}.get(
                self.column_kinds[index], 1
            ),
        )
        self.columns = [self.columns[index] for index in order]
        self.column_kinds = [self.column_kinds[index] for index in order]
        self.rows = [[row[index] for index in order] for row in self.rows]

    @classmethod
    def from_csv(cls, filename: str | Path) -> "TruthTable":
        with open(filename, "r", newline="", encoding="utf-8-sig") as stream:
            values = list(csv.reader(stream))
        if not values:
            raise ValueError("The CSV file is empty.")
        width = len(values[0])
        if width == 0 or any(len(row) != width for row in values):
            raise ValueError("Every CSV row must have the same number of columns.")
        return cls(list(values[0]), [list(row) for row in values[1:]])

    def to_csv(self, filename: str | Path) -> None:
        with open(filename, "w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(self.columns)
            writer.writerows(self.rows)

    def copy(self) -> "TruthTable":
        return TruthTable(
            self.columns.copy(),
            [row.copy() for row in self.rows],
            self.column_kinds.copy(),
            self.parameter_domains.copy(),
            self.parameter_positions.copy(),
        )

    def randomized(self, radix: int = 2, include_derived: bool = False) -> "TruthTable":
        if radix < 2:
            raise ValueError("The number system radix must be at least 2.")
        input_indices = [
            index for index, kind in enumerate(self.column_kinds) if kind == "input"
        ]
        output_indices = [
            index
            for index, kind in enumerate(self.column_kinds)
            if kind == "output" or (include_derived and kind == "derived")
        ]
        states = [str(value) for value in range(radix)]
        rows: list[list[str | None]] = []
        for input_values in itertools.product(states, repeat=len(input_indices)):
            row: list[str | None] = [None] * len(self.columns)
            for index, value in zip(input_indices, input_values):
                row[index] = value
            for index in output_indices:
                row[index] = random.choice(states)
            rows.append(row)
        return TruthTable(
            self.columns.copy(),
            rows,
            self.column_kinds.copy(),
            self.parameter_domains.copy(),
            self.parameter_positions.copy(),
        )

    def effective_rows(self) -> list[list[str | None]]:
        if not self.parameter_domains:
            return [row.copy() for row in self.rows]
        parameter_columns = set(self.parameter_domains)
        fallback_index = next(
            (index for index, kind in enumerate(self.column_kinds) if kind == "output"),
            len(self.columns),
        )
        positions = {
            column: self.parameter_positions.get(column, fallback_index + offset)
            for offset, column in enumerate(self.parameter_domains)
        }
        result: list[list[str | None]] = []
        for row in self.rows:
            effective_row: list[str | None] = []
            stored_index = 0
            for position in range(len(self.columns) + len(parameter_columns)):
                parameter = next(
                    (column for column, index in positions.items() if index == position),
                    None,
                )
                if parameter is None:
                    effective_row.append(row[stored_index])
                    stored_index += 1
                else:
                    effective_row.append(parameter)
            result.append(effective_row)
        return result


@dataclass
class LogicPort:
    name: str
    direction: str
    column: str | None = None
    values: tuple[str | None, ...] | None = None


@dataclass
class LogicNode:
    node_id: str
    kind: str
    truth_table: TruthTable | None = None
    inputs: list[LogicPort] = field(default_factory=list)
    outputs: list[LogicPort] = field(default_factory=list)
    is_imvolution: bool = False


@dataclass
class LogicEdge:
    edge_id: str
    source_node: str
    source_port: str
    target_node: str
    target_port: str
    column: str | None = None


_node_number = 0


@dataclass
class LogicGraph:
    nodes: dict[str, LogicNode] = field(default_factory=dict)
    edges: dict[str, LogicEdge] = field(default_factory=dict)
    pairing_loss: list[float] = field(default_factory=list)
    pairing_labels: list[str] = field(default_factory=list)

    def add_node(self, node: LogicNode) -> LogicNode:
        if node.node_id in self.nodes:
            raise ValueError(f"A node named {node.node_id!r} already exists.")
        self.nodes[node.node_id] = node
        return node

    @staticmethod
    def generate_node(
        kind: str,
        truth_table: TruthTable | None = None,
        inputs: list[LogicPort] | None = None,
        outputs: list[LogicPort] | None = None,
    ) -> LogicNode:
        global _node_number
        node_id = f"{kind}_{_node_number}"
        _node_number += 1
        return LogicNode(node_id, kind, truth_table, inputs or [], outputs or [])

    def remove_node(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        self.edges = {
            edge_id: edge
            for edge_id, edge in self.edges.items()
            if edge.source_node != node_id and edge.target_node != node_id
        }

    def connect(
        self,
        source_node: str,
        source_port: str,
        target_node: str,
        target_port: str,
        column: str | None = None,
    ) -> LogicEdge:
        source = next(
            port for port in self.nodes[source_node].outputs if port.name == source_port
        )
        target = next(
            port for port in self.nodes[target_node].inputs if port.name == target_port
        )
        if source.values is None:
            raise ValueError("Output ports must have truth-table values before connecting.")
        if target.values is not None and tuple(source.values) != tuple(target.values):
            raise ValueError("Output and input ports must have identical truth-table values.")
        edge_id = f"e{len(self.edges) + 1}"
        while edge_id in self.edges:
            edge_id += "x"
        target.values = source.values
        edge = LogicEdge(edge_id, source_node, source_port, target_node, target_port, column)
        self.edges[edge_id] = edge
        return edge

    def clone(self) -> "LogicGraph":
        result = LogicGraph(
            pairing_loss=self.pairing_loss.copy(),
            pairing_labels=self.pairing_labels.copy(),
        )
        for node in self.nodes.values():
            result.add_node(
                LogicNode(
                    node.node_id,
                    node.kind,
                    node.truth_table.copy() if node.truth_table else None,
                    [LogicPort(p.name, p.direction, p.column, p.values) for p in node.inputs],
                    [LogicPort(p.name, p.direction, p.column, p.values) for p in node.outputs],
                    node.is_imvolution,
                )
            )
        result.edges = {
            edge_id: LogicEdge(**vars(edge)) for edge_id, edge in self.edges.items()
        }
        return result
