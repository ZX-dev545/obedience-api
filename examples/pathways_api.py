"""API-backed GUI for prescribing and displaying logic-circuit pathways.

This is a standalone UI clone of ``pathways.py``. It keeps the UI graph and
truth-table presentation models, but submits solver requests through
``obedience_api.ObedienceApiClient`` instead of running a local solver.
"""

from __future__ import annotations

import csv
import json
import os
import math
import queue
import re
import threading
import time
import tkinter as tk
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Iterable, Sequence

from obedience_api import (
    LogicEdge,
    LogicGraph,
    LogicNode,
    LogicPort,
    ObedienceApiClient,
    ObedienceApiError,
    F_STRING_PLACEHOLDER,
    TruthTable,
)

# ---------------------------------------------------------------------------
# UI-independent graph and truth-table data structures

# Keep the placeholder syntax in one place so it can be changed without
# touching truth-table propagation.
IS_F_STRING = re.compile(r"^f(['\"])(.*)\1$")


def replace_f_string_placeholders(
    value: str, replace: Callable[[str], str], *, wrap_placeholders: bool = True
) -> str:
    """Replace balanced top-level placeholders, including nested f-strings."""
    result: list[str] = []
    position = 0
    while position < len(value):
        if value[position] != "{":
            result.append(value[position])
            position += 1
            continue
        depth = 1
        end = position + 1
        while end < len(value) and depth:
            if value[end] == "{":
                depth += 1
            elif value[end] == "}":
                depth -= 1
            end += 1
        if depth:
            result.append(value[position:])
            break
        replacement = replace(value[position + 1:end - 1])
        result.append(
            "{" + replacement + "}" if wrap_placeholders else replacement
        )
        position = end
    return "".join(result)


def render_output_template(
    value: str | None,
    input_columns: Sequence[str | None],
    input_values: Sequence[str | None],
) -> str | None:
    """Render a restricted f-string template using the current input values.

    Output cells that do not begin with ``f"`` or ``f'`` remain unchanged.
    Placeholders use input column names, for example ``f"state={Input A}"``.
    This deliberately supports substitutions only; arbitrary Python expressions
    are not evaluated.
    """
    if value is None or len(value) < 3 or value[0].lower() != "f":
        return value
    quote = value[1]
    if quote not in {"'", '"'} or value[-1] != quote:
        raise ValueError(
            "Invalid f-string output. Use f\"text {Input Column}\" or f'text {Input Column}'."
        )
    values_by_column = dict(zip(input_columns, input_values))

    def replace_placeholder(match: re.Match[str]) -> str:
        column = match.group(1).strip()
        if column.startswith("\\"):
            return match.group(0)
        if column not in values_by_column:
            return column
        return "" if values_by_column[column] is None else str(values_by_column[column])

    body = value[2:-1]
    rendered = F_STRING_PLACEHOLDER.sub(replace_placeholder, body)
    return rendered.replace("{{", "{").replace("}}", "}")


def template_columns(
    value: str | None, available_columns: Sequence[str] | None = None
) -> tuple[str, ...]:
    """Return input-column names referenced by an output template."""
    if value is None or len(value) < 3 or value[0].lower() != "f":
        return ()
    names = []
    for match in F_STRING_PLACEHOLDER.finditer(value[2:-1]):
        name = match.group(1).strip()
        if name.startswith("\\") or not name.isidentifier():
            continue
        if available_columns is not None and name not in available_columns:
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


def substitute_template_columns(value: str, replacements: tuple[str, ...]) -> str:
    if value is None or len(value) < 3 or value[0].lower() != "f":
        return ()
    content = value[2:-1]
    iterator = iter(replacements)
    content = F_STRING_PLACEHOLDER.sub(lambda match: next(iterator), content)
    return content


def substitute_known_template_columns(
    value: str | None,
    row: Sequence[str | None],
    columns: Sequence[str],
    *,
    preserve_f_string: bool = False,
) -> str | None:
    """Substitute known columns while preserving escaped local wildcards."""
    if value is None or not IS_F_STRING.match(str(value)):
        return value
    values = dict(zip(columns, row))

    def replace(name: str) -> str:
        name = name.strip()
        if name.startswith("\\") or name not in values:
            return "{" + name + "}" if not preserve_f_string else name
        replacement = "" if values[name] is None else str(values[name])
        return replacement

    quote = str(value)[1]
    body = str(value)[2:-1]
    substituted = replace_f_string_placeholders(
        body, replace, wrap_placeholders=preserve_f_string
    )
    if not preserve_f_string and "\\" in substituted:
        return str(value)
    if preserve_f_string:
        return f"{str(value)[0]}{quote}{substituted}{quote}"
    return substituted


def semantic_state_value(value: str | None) -> str | None:
    """Return the rendered value used when comparing literal state forms."""
    if value is None or not IS_F_STRING.match(str(value)):
        return value
    text = str(value)
    body = text[2:-1]
    placeholders = list(F_STRING_PLACEHOLDER.finditer(body))
    if any(match.group(1).strip().startswith("\\") for match in placeholders):
        return value
    return replace_f_string_placeholders(
        body,
        lambda expression: expression.strip(),
        wrap_placeholders=False,
    )


def semantic_state_key(values: Sequence[str | None]) -> tuple[str | None, ...]:
    """Build a dictionary key that treats literal f-strings as rendered states."""
    return tuple(semantic_state_value(value) for value in values)


def preferred_state_value(
    current: str | None, candidate: str | None
) -> str | None:
    """Keep the most informative representation of an equivalent state."""
    if current is None:
        return candidate
    if candidate is None:
        return current
    current_f_string = IS_F_STRING.match(str(current))
    candidate_f_string = IS_F_STRING.match(str(candidate))
    if candidate_f_string and not current_f_string:
        return candidate
    if current_f_string and not candidate_f_string:
        return current
    if current_f_string and candidate_f_string:
        current_has_wildcard = "\\" in str(current)
        candidate_has_wildcard = "\\" in str(candidate)
        if current_has_wildcard and not candidate_has_wildcard:
            return candidate
        if not current_has_wildcard and candidate_has_wildcard:
            return current
    return current


def match_local_wildcards(
    pattern: str | None, value: str | None
) -> dict[str, str] | None:
    """Match an f-string wildcard pattern against one concrete state."""
    if pattern is None or value is None:
        return {} if pattern == value else None
    pattern_text = str(pattern)
    value_text = str(value)
    pattern_match = IS_F_STRING.match(pattern_text)
    value_match = IS_F_STRING.match(value_text)
    pattern_body = pattern_match.group(2) if pattern_match else pattern_text
    value_body = value_match.group(2) if value_match else value_text
    if value_match:
        value_body = replace_f_string_placeholders(
            value_body,
            lambda expression: (
                expression.strip()
                if not expression.strip().startswith("\\")
                else "{" + expression + "}"
            ),
            wrap_placeholders=False,
        )
    matches = list(F_STRING_PLACEHOLDER.finditer(pattern_body))
    wildcard_matches = [
        match for match in matches if match.group(1).strip().startswith("\\")
    ]
    if not wildcard_matches:
        return {} if pattern_body == value_body else None

    expression = "^"
    position = 0
    names: list[str] = []
    for match in wildcard_matches:
        expression += re.escape(pattern_body[position:match.start()])
        name = match.group(1).strip()[1:]
        names.append(name)
        expression += "(.*?)"
        position = match.end()
    expression += re.escape(pattern_body[position:]) + "$"
    value_match = re.fullmatch(expression, value_body)
    if value_match is None:
        return None
    bindings: dict[str, str] = {}
    for name, captured in zip(names, value_match.groups()):
        if name in bindings and bindings[name] != captured:
            return None
        bindings[name] = captured
    return bindings


def substitute_local_wildcards(
    value: str | None, bindings: dict[str, str]
) -> str | None:
    """Render local wildcard placeholders while preserving f-string syntax."""
    if value is None or not bindings or not IS_F_STRING.match(str(value)):
        return value

    def replace(expression: str) -> str:
        expression = expression.strip()
        if expression.startswith("\\") and expression[1:] in bindings:
            return bindings[expression[1:]]
        return expression

    text = str(value)
    body = replace_f_string_placeholders(text[2:-1], replace)
    return f"{text[0]}{text[1]}{body}{text[-1]}"


def wildcard_specificity(value: str | None) -> tuple[int, int]:
    """Rank a pattern by literal specificity, preferring more fixed text."""
    if value is None or not IS_F_STRING.match(str(value)):
        return (-len(str(value)), 0)
    body = str(value)[2:-1]
    placeholders = list(F_STRING_PLACEHOLDER.finditer(body))
    wildcard_count = sum(
        match.group(1).strip().startswith("\\") for match in placeholders
    )
    literal_length = sum(
        len(body[start:end])
        for start, end in [(0, placeholders[0].start())]
    ) if placeholders else len(body)
    if placeholders:
        literal_length += sum(
            len(body[previous.end():current.start()])
            for previous, current in zip(placeholders, placeholders[1:])
        )
        literal_length += len(body[placeholders[-1].end():])
    return (-literal_length, wildcard_count)


@dataclass
class UINode:
    """Tk-facing presentation state for one logical node."""

    logic_node: LogicNode
    x: float = 0.0
    y: float = 0.0
    label: str | None = None

    def __post_init__(self) -> None:
        if self.label is None:
            self.label = self.logic_node.node_id


class ReadOnlyTable(ttk.Frame):
    """Spreadsheet-style truth table used by inspection dialogs."""

    def __init__(self, master: tk.Misc, table: TruthTable) -> None:
        super().__init__(master)
        tree = ttk.Treeview(self, show="headings", selectmode="none", height=min(12, max(3, len(table.rows))))
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.configure(yscrollcommand=scrollbar.set)
        tree["columns"] = table.columns
        for column in table.columns:
            tree.heading(column, text=column)
            tree.column(column, width=100, anchor=tk.CENTER, stretch=False)
        for row in table.rows:
            tree.insert("", tk.END, values=["" if value is None else value for value in row])


def show_inspection_dialog(parent: tk.Misc, title: str, item: object,
                           label: str | None = None,
                           edge_values: tuple[str | None, ...] = ()) -> None:
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.geometry("760x650")
    form = ttk.Frame(dialog, padding=14)
    form.pack(fill=tk.BOTH, expand=True)
    if isinstance(item, LogicNode):
        ttk.Label(form, text="Node inspection", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(form, text=f"ID: {item.node_id}\nType: {item.kind}\nLabel: {label or item.node_id}",
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 10))

        state_ports = [
            ("Input: " + port.name, port) for port in item.inputs
        ] + [
            ("Output: " + port.name, port) for port in item.outputs
        ]
        state_ports = [(name, port) for name, port in state_ports if port.values is not None]
        state_frame = ttk.LabelFrame(form, text="Propagated time-series states", padding=6)
        state_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        if state_ports:
            state_length = max(len(port.values or ()) for _, port in state_ports)
            state_table = TruthTable(
                ["Time"] + [name for name, _ in state_ports],
                [
                    [str(index)] + [
                        (port.values[index] if index < len(port.values or ()) else None)
                        for _, port in state_ports
                    ]
                    for index in range(state_length)
                ],
                ["input"] + ["output"] * len(state_ports),
            )
            ReadOnlyTable(state_frame, state_table).pack(fill=tk.BOTH, expand=True)
        else:
            ttk.Label(state_frame, text="No propagated state series available.").pack(anchor=tk.W)

        if item.truth_table:
            truth_frame = ttk.LabelFrame(form, text="Gate truth table", padding=6)
            truth_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
            ReadOnlyTable(truth_frame, item.truth_table).pack(fill=tk.BOTH, expand=True)
            if item.truth_table.parameter_domains:
                domains = "\n".join(
                    f"{column}: {', '.join(values) if values else 'any state'}"
                    for column, values in item.truth_table.parameter_domains.items()
                )
                ttk.Label(
                    form, text=f"Parameter domains:\n{domains}",
                    foreground="#777777", justify=tk.LEFT,
                ).pack(anchor=tk.W, pady=(0, 10))
        else:
            ttk.Label(form, text="No gate truth table").pack(anchor=tk.W, pady=(0, 10))
    else:
        edge = item
        ttk.Label(form, text="Edge inspection", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            form,
            text=(f"ID: {edge.edge_id}\n"
                  f"Source: {edge.source_node}.{edge.source_port}\n"
                  f"Target: {edge.target_node}.{edge.target_port}\n"
                  f"Column name: {edge.column or 'unspecified'}"),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 10))
        values_table = TruthTable([edge.column or "values"], [[value] for value in edge_values])
        ttk.Label(form, text="Column values", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        ReadOnlyTable(form, values_table).pack(fill=tk.BOTH, expand=True, pady=(4, 10))
    ttk.Button(form, text="Close", command=dialog.destroy).pack(anchor=tk.E)

class TableEditor(ttk.Frame):
    """Editable Treeview table with CSV import/export and a simple add-row UI."""

    def __init__(self, master: tk.Misc, table: TruthTable | None = None,
                 on_change: Callable[[], None] | None = None,
                 derived_visibility: dict[str, bool] | None = None,
                 style_name: str = "Pathways.Treeview",
                 parameter_domain: bool = False) -> None:
        super().__init__(master)
        self.table = table or TruthTable(["A", "OUT"], [["0", "0"], ["1", "1"]])
        self.table.sort_columns()
        self.on_change = on_change
        self.parameter_domain = parameter_domain
        self.derived_visibility = derived_visibility if derived_visibility is not None else {}
        self.active_column = 0
        self.active_item: str | None = None
        style = ttk.Style(self)
        try:
            has_layout = bool(style.layout(style_name))
        except tk.TclError:
            has_layout = False
        if not has_layout:
            style.layout(style_name, style.layout("Treeview"))
        style.configure(style_name, borderwidth=1, relief="solid", rowheight=24)
        style.map(style_name, background=[("selected", "#0078d7")], foreground=[("selected", "white")])
        self.tree = ttk.Treeview(self, show="headings", selectmode="browse",
                                 style=style_name, takefocus=True)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Double-1>", self._edit_header_or_cell)
        self.tree.bind("<Button-1>", self._focus_cell, add="+")
        self.tree.bind("<Key>", self._navigate_or_edit)
        if self.parameter_domain:
            self.tree.bind("<Delete>", self._delete_selected_row)
            self._normalize_parameter_domain()
        self.refresh()

    def _delete_selected_row(self, _event: tk.Event) -> str:
        if self.tree.selection():
            self.delete_row()
        return "break"

    def _normalize_parameter_domain(self) -> None:
        if not self.parameter_domain:
            return
        table = self.read_table() if self.tree.get_children() else self.table.copy()
        values: list[str] = []
        for row in table.rows:
            value = row[0] if row else None
            if value not in (None, "") and value not in values:
                values.append(value)
        table.rows = [[value] for value in values] + [[None]]
        self.table = table

    def refresh(self) -> None:
        self.table.sort_columns()
        self.active_item = None
        self.tree.selection_remove(self.tree.selection())
        self.tree.focus("")
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = self.table.columns
        parameter_columns = {
            column for row in self.table.rows
            for column in template_columns(
                row[-1] if row else None,
                self.table.columns + list(self.table.parameter_domains),
            )
        }
        for index, column in enumerate(self.table.columns):
            heading = f"> {column}" if index == self.active_column else column
            if self.table.column_kinds[index] == "derived":
                heading = f"%{heading}"
            if column in parameter_columns:
                heading = f"◇ {heading}"
            self.tree.heading(column, text=heading)
            visible = self.derived_visibility.get(column, True)
            self.tree.column(column, width=92 if visible else 0, minwidth=0,
                             anchor=tk.CENTER, stretch=False)
        for row_index, row in enumerate(self.table.rows):
            display_row = list(row)
            if (
                self.parameter_domain
                and row_index == len(self.table.rows) - 1
                and display_row
                and display_row[0] is None
            ):
                display_row[0] = "Add State"
            self.tree.insert("", tk.END, values=display_row)

    def read_table(self) -> TruthTable:
        rows = [
            [
                value if value not in ("", "Add State") else None
                for value in self.tree.item(item, "values")
            ]
            for item in self.tree.get_children()
        ]
        return TruthTable(list(self.tree["columns"]), rows, self.table.column_kinds.copy())

    def _edit_cell(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if not item or not column_id:
            return
        self._edit_cell_at(item, int(column_id[1:]) - 1)

    def _edit_cell_at(self, item: str, column_index: int, initial: str | None = None) -> None:
        if self.table.column_kinds[column_index] == "derived":
            return
        self.active_item = item
        self.active_column = column_index
        self.tree.selection_set(item)
        self.tree.focus(item)
        column_id = f"#{column_index + 1}"
        value = self.tree.item(item, "values")[column_index]
        if self.parameter_domain and value == "Add State":
            value = ""
        editor = ttk.Entry(self.tree)
        editor.insert(0, value if initial is None else initial)
        bbox = self.tree.bbox(item, column_id)
        if not bbox:
            return
        editor.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        editor.focus_set()
        editor.icursor(tk.END)

        def commit(edit_event: tk.Event | None = None) -> None:
            items_before_refresh = list(self.tree.get_children())
            row_index = items_before_refresh.index(item) if item in items_before_refresh else 0
            values = list(self.tree.item(item, "values"))
            values[column_index] = editor.get()
            self.tree.item(item, values=values)
            self.table = self.read_table()
            editor.destroy()
            self._normalize_parameter_domain()
            self.refresh()
            if self.on_change:
                self.on_change()
            if edit_event and edit_event.keysym == "Return":
                self.tree.focus_set()
                self._focus_location(row_index, column_index)
                self.refresh_headings()

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)

    def _focus_cell(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if item and column_id:
            self.active_item = item
            self.active_column = int(column_id[1:]) - 1
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.refresh_headings()

    def is_derived(self, column_index: int) -> bool:
        return self.table.column_kinds[column_index] == "derived"

    def set_derived_visibility(self, column: str, visible: bool) -> None:
        self.derived_visibility[column] = visible
        self.refresh()

    def refresh_headings(self) -> None:
        for index, column in enumerate(self.table.columns):
            self.tree.heading(column, text=f"> {column}" if index == self.active_column else column)

    def _navigate_or_edit(self, event: tk.Event) -> str | None:
        if not self.tree.focus():
            items = self.tree.get_children()
            if not items:
                return "break"
            self.active_item = items[0]
            self.active_column = 0
            self.tree.focus(self.active_item)
            self.tree.selection_set(self.active_item)
        item = self.tree.focus()
        items = list(self.tree.get_children())
        row_index = items.index(item)
        column_index = self.active_column
        shift_pressed = bool(event.state & 0x1)
        if event.keysym == "Left":
            if column_index == 0 and shift_pressed:
                self.insert_column(0, "input")
                self._focus_location(row_index, 0)
                return "break"
            column_index = max(0, column_index - 1)
        elif event.keysym == "Right":
            if column_index == len(self.table.columns) - 1 and shift_pressed:
                self.insert_column(len(self.table.columns), "output")
                self._focus_location(row_index, column_index + 1)
                return "break"
            column_index = min(len(self.table.columns) - 1, column_index + 1)
        elif event.keysym == "Up":
            if row_index == 0 and shift_pressed:
                self.insert_row(0)
                self._focus_location(0, column_index)
                return "break"
            row_index = max(0, row_index - 1)
        elif event.keysym == "Down":
            if row_index == len(items) - 1 and shift_pressed:
                self.insert_row(len(items))
                self._focus_location(row_index + 1, column_index)
                return "break"
            row_index = min(len(items) - 1, row_index + 1)
        elif event.keysym in {"Return", "F2"}:
            if self.is_derived(column_index):
                return "break"
            self._edit_cell_at(item, column_index)
            return "break"
        elif event.char and event.char.isprintable() and not event.state & 0x4:
            if self.is_derived(column_index):
                return "break"
            self._edit_cell_at(item, column_index, event.char)
            return "break"
        else:
            return None
        new_item = items[row_index]
        self.active_item = new_item
        self.active_column = column_index
        self.tree.focus(new_item)
        self.tree.selection_set(new_item)
        self.tree.see(new_item)
        self.refresh_headings()
        return "break"

    def _focus_location(self, row_index: int, column_index: int) -> None:
        items = list(self.tree.get_children())
        if not items:
            return
        self.active_item = items[max(0, min(row_index, len(items) - 1))]
        self.active_column = max(0, min(column_index, len(self.table.columns) - 1))
        self.tree.focus(self.active_item)
        self.tree.selection_set(self.active_item)
        self.tree.see(self.active_item)
        self.refresh_headings()

    def _selected_location(self) -> tuple[int, int]:
        items = list(self.tree.get_children())
        if not items:
            return 0, max(0, self.active_column)
        item = self.tree.focus()
        if item not in items:
            item = self.active_item
        if item not in items:
            item = items[0]
        return items.index(item), max(0, min(self.active_column, len(self.table.columns) - 1))

    def insert_row(self, index: int | None = None) -> None:
        self.table = self.read_table()
        if index is None:
            index, _ = self._selected_location()
        self.table.rows.insert(max(0, min(index, len(self.table.rows))), [None] * len(self.table.columns))
        self._normalize_parameter_domain()
        self.refresh()
        self._focus_location(index, self.active_column)
        if self.on_change:
            self.on_change()

    def delete_row(self) -> None:
        self.table = self.read_table()
        if len(self.table.rows) <= 1:
            return
        index, column_index = self._selected_location()
        del self.table.rows[index]
        self._normalize_parameter_domain()
        self.refresh()
        self._focus_location(min(index, len(self.table.rows) - 1), column_index)
        if self.on_change:
            self.on_change()

    def insert_column(self, index: int | None = None, kind: str = "input") -> None:
        self.table = self.read_table()
        if index is None:
            row_index, index = self._selected_location()
        else:
            row_index, _ = self._selected_location()
        index = max(0, min(index, len(self.table.columns)))
        prefix = "input" if kind == "input" else "output"
        name = self._next_column_name(prefix)
        self.table.columns.insert(index, name)
        self.table.column_kinds.insert(index, kind)
        for row in self.table.rows:
            row.insert(index, None)
        self.refresh()
        self._focus_location(row_index, index)
        if self.on_change:
            self.on_change()

    def delete_column(self) -> None:
        if len(self.table.columns) <= 1:
            return
        self.table = self.read_table()
        row_index, column_index = self._selected_location()
        del self.table.columns[column_index]
        del self.table.column_kinds[column_index]
        for row in self.table.rows:
            del row[column_index]
        self.refresh()
        self._focus_location(row_index, min(column_index, len(self.table.columns) - 1))
        if self.on_change:
            self.on_change()

    def _edit_header_or_cell(self, event: tk.Event) -> None:
        if self.tree.identify_region(event.x, event.y) == "heading":
            column_id = self.tree.identify_column(event.x)
            if column_id:
                column_index = int(column_id[1:]) - 1
                self.rename_column(column_index)
            return
        self._edit_cell(event)

    def _next_column_name(self, prefix: str) -> str:
        number = 1
        while f"{prefix}{number}" in self.table.columns:
            number += 1
        return f"{prefix}{number}"

    def add_column(self, kind: str) -> None:
        self.insert_column(kind=kind)

    def rename_column(self, column_index: int) -> None:
        if not 0 <= column_index < len(self.table.columns):
            return
        current_name = self.table.columns[column_index]
        new_name = simpledialog.askstring(
            "Rename column",
            "Column name:",
            initialvalue=current_name,
            parent=self.winfo_toplevel(),
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        if not new_name:
            messagebox.showerror("Invalid column name", "Column names cannot be empty.", parent=self.winfo_toplevel())
            return
        if new_name in self.table.columns and new_name != current_name:
            messagebox.showerror("Duplicate column name", "Column names must be unique.", parent=self.winfo_toplevel())
            return
        self.table = self.read_table()
        self.table.columns[column_index] = new_name
        self.refresh()
        if self.on_change:
            self.on_change()

    def import_csv(self) -> None:
        filename = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*")])
        if filename:
            try:
                self.table = TruthTable.from_csv(filename)
                self.refresh()
                if self.on_change:
                    self.on_change()
            except (OSError, ValueError) as exc:
                messagebox.showerror("Import failed", str(exc))

    def add_row(self) -> None:
        self.insert_row()

    def get_table(self) -> TruthTable:
        return self.read_table()


class ParameterDomainEditor(ttk.Frame):
    """One independently expandable single-column editor per f-string parameter."""

    def __init__(self, master: tk.Misc,
                 on_change: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        ttk.Style(self).configure(
            "Pathways.ParameterTreeview", foreground="#777777"
        )
        self.on_change = on_change
        self.editors: dict[str, TableEditor] = {}
        self.frames: dict[str, ttk.Frame] = {}

    def set_domains(
        self,
        columns: Sequence[str],
        domains: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        columns = list(dict.fromkeys(columns))
        domains = {
            column: tuple(dict.fromkeys(
                value for value in domains.get(column, ())
                if value not in (None, "")
            ))
            for column in columns
        }
        if list(self.editors) == columns and self.get_domains() == domains:
            return
        for frame in self.frames.values():
            frame.destroy()
        self.editors.clear()
        self.frames.clear()
        for column in columns:
            frame = ttk.LabelFrame(self, text=column, padding=4)
            frame.configure(width=130, height=220)
            frame.pack_propagate(False)
            frame.pack(side=tk.LEFT, anchor=tk.N, padx=(0, 6))
            values = domains.get(column, ())
            table = TruthTable(
                [column],
                [[value] for value in values] + [[None]],
                ["input"],
            )
            editor = TableEditor(
                frame, table, on_change=self.on_change,
                style_name="Pathways.ParameterTreeview",
                parameter_domain=True,
            )
            editor.pack(fill=tk.BOTH, expand=True)
            controls = ttk.Frame(frame)
            controls.pack(fill=tk.X, pady=(4, 0))
            ttk.Button(
                controls, text="+", width=3,
                command=editor.add_row,
            ).pack(side=tk.LEFT)
            ttk.Button(
                controls, text="-", width=3,
                command=editor.delete_row,
            ).pack(side=tk.LEFT, padx=3)
            self.frames[column] = frame
            self.editors[column] = editor

    def get_domains(self) -> dict[str, tuple[str, ...]]:
        return {
            column: tuple(
                row[0] for row in table.rows
                if row and row[0] not in (None, "")
            )
            for column, editor in self.editors.items()
            for table in [editor.get_table()]
        }


class GraphCanvas(ttk.Frame):
    """Pan/zoom graph editor with node dragging and port-to-port connections."""

    def __init__(self, master: tk.Misc, graph: LogicGraph, editable: bool = True,
                 on_inspect: Callable[[str, object], None] | None = None,
                 on_message: Callable[[str], None] | None = None,
                 on_graph_change: Callable[[], None] | None = None,
                 circular_nodes: bool = False) -> None:
        super().__init__(master)
        self.graph = graph
        self.editable = editable
        self.on_inspect = on_inspect
        self.on_message = on_message
        self.on_graph_change = on_graph_change
        self.circular_nodes = circular_nodes
        self.views: dict[str, UINode] = {
            node_id: UINode(node) for node_id, node in graph.nodes.items()
        }
        self.canvas = tk.Canvas(self, background="#f5f7f4", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.scale = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.drag_node: str | None = None
        self.drag_start = (0.0, 0.0)
        self.pan_start: tuple[float, float] | None = None
        self.connect_start: tuple[str, str] | None = None
        self.selected_node: str | None = None
        self.selected_edge: str | None = None
        self.node_value_labels: dict[str, str] = {}
        self.node_value_details: dict[str, str] = {}
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._right_click)
        self.canvas.bind("<Button-2>", self._start_pan)
        self.canvas.bind("<B2-Motion>", self._pan)
        self.canvas.bind("<ButtonRelease-2>", self._stop_pan)
        self.canvas.bind("<MouseWheel>", self._zoom)
        self.canvas.bind("<Button-4>", self._zoom)
        self.canvas.bind("<Button-5>", self._zoom)
        self.canvas.bind("<Configure>", lambda _event: self.draw())

    def _screen(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale + self.pan_x, y * self.scale + self.pan_y

    def _world(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.pan_x) / self.scale, (y - self.pan_y) / self.scale

    def _view(self, node_id: str) -> UINode:
        node = self.graph.nodes[node_id]
        return self.views.setdefault(node_id, UINode(node))

    def view(self, node_id: str) -> UINode:
        return self._view(node_id)

    def screen_to_world(self, x: float, y: float) -> tuple[float, float]:
        return self._world(x, y)

    def reset_view(self) -> None:
        self.scale, self.pan_x, self.pan_y = 1.0, 0.0, 0.0
        self.pan_start = None
        self.draw()

    def _zoom(self, event: tk.Event) -> None:
        factor = 1.1 if getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4 else 0.9
        old_scale = self.scale
        self.scale = min(3.0, max(0.35, self.scale * factor))
        self.pan_x = event.x - (event.x - self.pan_x) * self.scale / old_scale
        self.pan_y = event.y - (event.y - self.pan_y) * self.scale / old_scale
        self.draw()

    def _start_pan(self, event: tk.Event) -> None:
        self.pan_start = (event.x, event.y)

    def _pan(self, event: tk.Event) -> None:
        if self.pan_start is None:
            return
        self.pan_x += event.x - self.pan_start[0]
        self.pan_y += event.y - self.pan_start[1]
        self.pan_start = (event.x, event.y)
        self.draw()

    def _stop_pan(self, _event: tk.Event) -> None:
        self.pan_start = None

    def _node_at(self, x: float, y: float) -> str | None:
        wx, wy = self._world(x, y)
        for node_id in reversed(list(self.graph.nodes)):
            view = self._view(node_id)
            if abs(wx - view.x) <= 58 and abs(wy - view.y) <= 30:
                return node_id
        return None

    def _port_at(self, x: float, y: float) -> tuple[str, str] | None:
        wx, wy = self._world(x, y)
        for node in self.graph.nodes.values():
            ports = node.inputs + node.outputs
            for port in ports:
                px, py = self._port_position(node.node_id, port)
                if (wx - px) ** 2 + (wy - py) ** 2 <= 10 ** 2:
                    return node.node_id, port.name
        return None

    def _edge_at(self, x: float, y: float) -> str | None:
        wx, wy = self._world(x, y)
        for edge in self.graph.edges.values():
            source = self.graph.nodes.get(edge.source_node)
            target = self.graph.nodes.get(edge.target_node)
            if not source or not target:
                continue
            source_port = self._port(edge.source_node, edge.source_port, "output")
            target_port = self._port(edge.target_node, edge.target_port, "input")
            if not source_port or not target_port:
                continue
            source_x, source_y = self._port_position(edge.source_node, source_port)
            target_x, target_y = self._port_position(edge.target_node, target_port)
            distance = self._distance_to_segment(wx, wy, source_x, source_y, target_x, target_y)
            if distance <= 9:
                return edge.edge_id
        return None

    @staticmethod
    def _distance_to_segment(x: float, y: float, x1: float, y1: float,
                             x2: float, y2: float) -> float:
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(x - x1, y - y1)
        position = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - (x1 + position * dx), y - (y1 + position * dy))

    def _port(self, node_id: str, port_name: str,
              direction: str | None = None) -> LogicPort | None:
        node = self.graph.nodes[node_id]
        ports = node.inputs + node.outputs
        return next((port for port in ports
                     if port.name == port_name and
                     (direction is None or port.direction == direction)), None)

    def _port_position(self, node_id: str, port: LogicPort) -> tuple[float, float]:
        node = self.graph.nodes[node_id]
        view = self._view(node_id)
        if node.is_imvolution:
            ux, uy = self._imvolution_direction(node_id)
            distance = -25 if port.direction == "input" else 32
            return view.x + ux * distance, view.y + uy * distance
        ports = node.inputs + node.outputs
        index = ports.index(port)
        radius = 34 if self.circular_nodes and not node.is_imvolution else 32 if node.is_imvolution else 58
        x = view.x - radius if port.direction == "input" else view.x + radius
        y = view.y + (index - (len(ports) - 1) / 2) * 14
        return x, y

    def _imvolution_direction(self, node_id: str) -> tuple[float, float]:
        node = self.graph.nodes[node_id]
        incoming = next((edge for edge in self.graph.edges.values()
                         if edge.target_node == node_id), None)
        outgoing = next((edge for edge in self.graph.edges.values()
                         if edge.source_node == node_id), None)
        if incoming and outgoing:
            source_view = self._view(incoming.source_node)
            target_view = self._view(outgoing.target_node)
            dx = target_view.x - source_view.x
            dy = target_view.y - source_view.y
            length = math.hypot(dx, dy)
            if length:
                return dx / length, dy / length
        return 1.0, 0.0

    def _press(self, event: tk.Event) -> None:
        port = self._port_at(event.x, event.y)
        if self.editable and port:
            self.connect_start = port
            self.selected_node = port[0]
            self.selected_edge = None
            self.draw()
            return
        node_id = self._node_at(event.x, event.y)
        if node_id:
            self.selected_node = node_id
            self.selected_edge = None
            self.draw()
            self.drag_node = node_id
            self.drag_start = (event.x, event.y)
            return
        edge_id = self._edge_at(event.x, event.y)
        self.selected_node = None
        self.selected_edge = edge_id
        self.draw()

    def _motion(self, event: tk.Event) -> None:
        if self.drag_node:
            view = self._view(self.drag_node)
            view.x += (event.x - self.drag_start[0]) / self.scale
            view.y += (event.y - self.drag_start[1]) / self.scale
            self.drag_start = (event.x, event.y)
            self.draw()
        elif self.connect_start:
            self.draw(connection_point=(event.x, event.y))

    def _release(self, event: tk.Event) -> None:
        if self.connect_start:
            target = self._port_at(event.x, event.y)
            if not target or target[0] == self.connect_start[0]:
                self._message("Connection cancelled: drop on a different port.")
            else:
                start_port = self._port(*self.connect_start)
                end_port = self._port(*target)
                if start_port and end_port:
                    source, destination = self.connect_start, target
                    source_port, target_port = start_port, end_port
                    if start_port.direction == "input" and end_port.direction == "output":
                        source, destination = target, self.connect_start
                        source_port, target_port = end_port, start_port
                    if (source_port.direction == "output" and target_port.direction == "input"
                            and source_port.values is not None
                            and (target_port.values is None or
                                 source_port.values == target_port.values)):
                        edge = self.graph.connect(source[0], source[1], destination[0], destination[1],
                                                  source_port.column)
                        self.selected_edge = edge.edge_id
                        self.selected_node = None
                        self._message(f"Connected column {source_port.column}.")
                        if self.on_graph_change:
                            self.on_graph_change()
                    elif source_port.direction != "output" or target_port.direction != "input":
                        self._message("Connection rejected: drag between an output port and an input port.")
                    else:
                        self._message(
                            "Connection rejected: "
                            f"{source_port.name} values {list(source_port.values or ())} "
                            f"!= {target_port.name} values {list(target_port.values or ())}."
                        )
            self.connect_start = None
        self.drag_node = None
        self.draw()

    def _port_column(self, node_id: str, port_name: str) -> str | None:
        node = self.graph.nodes[node_id]
        for port in node.inputs + node.outputs:
            if port.name == port_name:
                return port.column
        return None

    def _right_click(self, event: tk.Event) -> None:
        port = self._port_at(event.x, event.y)
        node_id = self._node_at(event.x, event.y)
        selected: object | None = None
        item_type: str | None = None
        item_id: str | None = None
        if node_id:
            selected = self.graph.nodes[node_id]
            item_type, item_id = "node", node_id
        elif port:
            selected = next((edge for edge in self.graph.edges.values()
                             if (edge.source_node, edge.source_port) == port or
                             (edge.target_node, edge.target_port) == port), None)
            if selected:
                item_type, item_id = "edge", selected.edge_id
        else:
            edge_id = self._edge_at(event.x, event.y)
            selected = self.graph.edges.get(edge_id) if edge_id else None
            if selected:
                item_type, item_id = "edge", edge_id
        if selected and item_type and item_id:
            self.selected_node = item_id if item_type == "node" else None
            self.selected_edge = item_id if item_type == "edge" else None
            self.draw()
            self._show_context_menu(event, item_type, item_id)

    def _show_context_menu(self, event: tk.Event, item_type: str, item_id: str) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Inspect", command=lambda: self._inspect_item(item_type, item_id))
        can_delete = self.editable and (
            item_type == "edge"
            or (item_type == "node" and self.graph.nodes[item_id].kind == "gate")
        )
        menu.add_command(label="Delete", command=lambda: self._delete_item(item_type, item_id),
                         state=tk.NORMAL if can_delete else tk.DISABLED)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _inspect_item(self, item_type: str, item_id: str) -> None:
        if not self.on_inspect:
            return
        item = self.graph.nodes.get(item_id) if item_type == "node" else self.graph.edges.get(item_id)
        if item:
            self.on_inspect("Inspect", item)

    def _delete_item(self, item_type: str, item_id: str) -> None:
        if not self.editable:
            return
        if item_type == "edge":
            self.graph.edges.pop(item_id, None)
            self.selected_edge = None
        elif item_type == "node" and self.graph.nodes.get(item_id, None) and self.graph.nodes[item_id].kind == "gate":
            self.graph.remove_node(item_id)
            self.selected_node = None
        self.draw()
        if self.on_graph_change:
            self.on_graph_change()

    def _message(self, text: str) -> None:
        if self.on_message:
            self.on_message(text)

    def draw(self, connection_point: tuple[float, float] | None = None) -> None:
        self.canvas.delete("all")
        for edge in self.graph.edges.values():
            source = self.graph.nodes.get(edge.source_node)
            target = self.graph.nodes.get(edge.target_node)
            if not source or not target:
                continue
            source_port = self._port(edge.source_node, edge.source_port, "output")
            target_port = self._port(edge.target_node, edge.target_port, "input")
            if not source_port or not target_port:
                continue
            source_x, source_y = self._port_position(edge.source_node, source_port)
            target_x, target_y = self._port_position(edge.target_node, target_port)
            x1, y1 = self._screen(source_x, source_y)
            x2, y2 = self._screen(target_x, target_y)
            selected = edge.edge_id == self.selected_edge
            self.canvas.create_line(
                x1, y1, x2, y2,
                fill="#0078d7" if selected else "#65756a",
                width=4 if selected else 2,
                arrow=tk.LAST,
            )
            self.canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2 - 8,
                                    text=edge.column or "?", fill="#53635a", font=("Segoe UI", 8))
        if connection_point and self.connect_start:
            source_port = self._port(*self.connect_start)
            if source_port:
                start_x, start_y = self._port_position(self.connect_start[0], source_port)
                x1, y1 = self._screen(start_x, start_y)
                self.canvas.create_line(x1, y1, connection_point[0], connection_point[1],
                                        fill="#0078d7", width=2, dash=(6, 3))
        for node in self.graph.nodes.values():
            view = self._view(node.node_id)
            x, y = self._screen(view.x, view.y)
            fill = {"input": "#c7e3d2", "output": "#f6d59a", "gate": "#cbd7ed"}.get(node.kind, "white")
            selected = node.node_id == self.selected_node
            outline = "#0078d7" if selected else "#31443a"
            width = 4 if selected else 2
            if getattr(node, "is_imvolution", False):
                ux, uy = self._imvolution_direction(node.node_id)
                px, py = -uy, ux
                tip = (x + ux * 32 * self.scale, y + uy * 32 * self.scale)
                left = (x - ux * 25 * self.scale + px * 25 * self.scale,
                        y - uy * 25 * self.scale + py * 25 * self.scale)
                right = (x - ux * 25 * self.scale - px * 25 * self.scale,
                         y - uy * 25 * self.scale - py * 25 * self.scale)
                self.canvas.create_polygon(*tip, *left, *right,
                                            fill=fill, outline=outline, width=width)
            elif self.circular_nodes:
                self.canvas.create_oval(
                    x - 34 * self.scale, y - 34 * self.scale,
                    x + 34 * self.scale, y + 34 * self.scale,
                    fill=fill, outline=outline, width=width,
                )
            else:
                self.canvas.create_rectangle(
                    x - 58 * self.scale, y - 30 * self.scale,
                    x + 58 * self.scale, y + 30 * self.scale,
                    fill=fill, outline=outline, width=width,
                )
            node_label = self.node_value_labels.get(node.node_id, view.label)
            value_detail = self.node_value_details.get(node.node_id)
            if value_detail:
                self.canvas.create_text(
                    x, y - 8 * self.scale, text=node_label,
                    font=("Segoe UI", 10, "bold"),
                )
                self.canvas.create_text(
                    x, y + 9 * self.scale, text=value_detail,
                    fill="#c62828" if node.kind == "gate" else "#31443a",
                    font=("Segoe UI", 8, "bold"),
                )
            else:
                self.canvas.create_text(x, y, text=node_label, font=("Segoe UI", 10, "bold"))
            ports = node.inputs + node.outputs
            for port in ports:
                port_x, port_y = self._port_position(node.node_id, port)
                px, py = self._screen(port_x, port_y)
                self.canvas.create_oval(px - 5, py - 5, px + 5, py + 5,
                                        fill="#4c8c67" if port.direction == "input" else "#c35c50",
                                        outline="#26372d")
                self.canvas.create_text(px + (-9 if port.direction == "input" else 9), py,
                                        text=port.name, anchor="e" if port.direction == "input" else "w",
                                        font=("Segoe UI", 8))

    def set_node_value_labels(
        self, labels: dict[str, str], values: dict[str, str] | None = None
    ) -> None:
        self.node_value_labels = labels.copy()
        self.node_value_details = (values or {}).copy()
        self.draw()


class SolverDiagnostics(ttk.Frame):
    """Pairing-loss plot for one scenario."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=8)
        self.losses: Sequence[float] = ()
        self.loss_labels: Sequence[str] = ()
        ttk.Label(
            self, text="Solver pairing diagnostics",
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor=tk.W, pady=(4, 8))
        self.loss_zoom = 1.0
        plot_frame = ttk.Frame(self)
        plot_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self.loss_canvas = tk.Canvas(plot_frame, height=300, background="white", highlightthickness=1)
        horizontal = ttk.Scrollbar(plot_frame, orient=tk.HORIZONTAL, command=self.loss_canvas.xview)
        vertical = ttk.Scrollbar(plot_frame, orient=tk.VERTICAL, command=self.loss_canvas.yview)
        self.loss_canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.loss_canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(0, weight=1)
        self.loss_canvas.bind("<MouseWheel>", self._zoom_loss)
        self.loss_canvas.bind("<Configure>", lambda _event: self._draw_loss(self.losses))
        self.loss_label = ttk.Label(self, text="No pairing loss data recorded.")
        self.loss_label.pack(anchor=tk.W)

    def _zoom_loss(self, event: tk.Event) -> None:
        self.loss_zoom = min(3.0, max(0.75, self.loss_zoom * (1.1 if event.delta > 0 else 1 / 1.1)))
        self._draw_loss(self.losses)

    def update_graph(self, graph: LogicGraph) -> None:
        self.losses = graph.pairing_loss
        self.loss_labels = graph.pairing_labels
        self._draw_loss(self.losses)

    def _draw_loss(self, losses: Sequence[float]) -> None:
        self.loss_canvas.delete("all")
        width = max(1, int(self.loss_canvas.winfo_width() * self.loss_zoom))
        height = max(1, int(self.loss_canvas.winfo_height() * self.loss_zoom))
        self.loss_canvas.configure(scrollregion=(0, 0, width, height))
        left, top, right, bottom = 55, 28, width - 16, height - 80
        self.loss_canvas.create_text(left, top, text="Pairing loss", anchor=tk.W, fill="#31443a")
        if not losses:
            self.loss_label.configure(text="No pairing loss data recorded.")
            return
        minimum, maximum = min(losses), max(losses)
        span = maximum - minimum or 1.0
        self.loss_canvas.create_text(
            10, (top + bottom) / 2, text="Loss", angle=90,
            fill="#31443a",
        )
        for tick in range(5):
            value = maximum - span * tick / 4
            y = top + (bottom - top) * tick / 4
            self.loss_canvas.create_line(left - 4, y, right, y, fill="#e1e7e2")
            self.loss_canvas.create_text(
                left - 7, y, text=f"{value:g}", anchor=tk.E,
                font=("Segoe UI", 8), fill="#53635a",
            )
        self.loss_canvas.create_text(
            (left + right) / 2, bottom + 28, text="Pairing iteration",
            fill="#31443a",
        )
        points = []
        for index, loss in enumerate(losses):
            x = left + (right - left) * index / max(1, len(losses) - 1)
            y = bottom - (bottom - top) * (loss - minimum) / span
            points.extend((x, y))
        self.loss_canvas.create_line(left, bottom, right, bottom, fill="#9aa79d")
        self.loss_canvas.create_line(left, top, left, bottom, fill="#9aa79d")
        if len(points) >= 4:
            self.loss_canvas.create_line(*points, fill="#0078d7", width=2)
        for index in range(len(losses)):
            x = left + (right - left) * index / max(1, len(losses) - 1)
            self.loss_canvas.create_oval(x - 3, points[index * 2 + 1] - 3,
                                         x + 3, points[index * 2 + 1] + 3,
                                         fill="#0078d7", outline="")
            self.loss_canvas.create_text(
                x, points[index * 2 + 1] - 9, text=f"{losses[index]:g}",
                anchor=tk.S, font=("Segoe UI", 8, "bold"), fill="#005a9e",
            )
        for index, label in enumerate(self.loss_labels[:len(losses)]):
            x = left + (right - left) * index / max(1, len(losses) - 1)
            y = points[index * 2 + 1]
            self.loss_canvas.create_text(
                x, y + 8, text=label, width=110, anchor=tk.N,
                justify=tk.CENTER, font=("Segoe UI", 7), fill="#31443a",
            )
        self.loss_label.configure(text=f"{len(losses)} pairing iteration(s); loss range {minimum:g}–{maximum:g}")

class TimeSeriesTable(ttk.Frame):
    """Horizontally scrollable propagated state table for one scenario."""

    def __init__(self, master: tk.Misc,
                 on_row_selected: Callable[[int], None] | None = None) -> None:
        super().__init__(master, padding=8)
        self.on_row_selected = on_row_selected
        self.selected_label = ttk.Label(self, text="Select a time-series row to inspect its propagated values.")
        self.selected_label.pack(anchor=tk.W, pady=(0, 4))
        table_frame = ttk.Frame(self)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self.table = ttk.Treeview(table_frame, show="headings", selectmode="browse")
        self.table.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        vertical_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.table.yview)
        vertical_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        horizontal_scrollbar = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.table.xview)
        horizontal_scrollbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.table.configure(
            yscrollcommand=vertical_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        self.table.bind("<<TreeviewSelect>>", self._row_selected)

    def update_graph(self, graph: LogicGraph) -> None:
        columns, rows = self._time_series(graph)
        self.table.delete(*self.table.get_children())
        self.table["columns"] = columns
        for column in columns:
            self.table.heading(column, text=column)
            self.table.column(column, width=120, minwidth=90, anchor=tk.CENTER, stretch=False)
        for row in rows:
            self.table.insert("", tk.END, values=["" if value is None else value for value in row])
        self.selected_label.configure(text="Select a time-series row to inspect its propagated values.")

    @staticmethod
    def _time_series(graph: LogicGraph) -> tuple[list[str], list[list[str | None]]]:
        series: list[tuple[str, tuple[str | None, ...]]] = []
        node_order = list(graph.nodes)
        order_index = {node_id: index for index, node_id in enumerate(node_order)}
        successors: dict[str, list[str]] = {node_id: [] for node_id in node_order}
        indegree = {node_id: 0 for node_id in node_order}
        for edge in graph.edges.values():
            if edge.target_node not in successors or edge.source_node not in successors:
                continue
            if edge.target_node not in successors[edge.source_node]:
                successors[edge.source_node].append(edge.target_node)
                indegree[edge.target_node] += 1
        ready = [
            node_id for node_id in node_order
            if indegree[node_id] == 0
        ]
        propagation_order: list[str] = []
        while ready:
            ready.sort(key=order_index.__getitem__)
            node_id = ready.pop(0)
            propagation_order.append(node_id)
            for successor in successors[node_id]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
        propagation_order.extend(
            node_id for node_id in node_order
            if node_id not in propagation_order
        )

        for node_id in propagation_order:
            node = graph.nodes[node_id]
            if node.kind == "gate":
                ports = node.outputs
            elif node.kind == "input":
                ports = node.outputs
            elif node.kind == "output":
                ports = node.inputs
            else:
                ports = []
            for port in ports:
                if port.values is not None:
                    series.append((f"{node.node_id}.{port.name}", port.values))
        length = max((len(values) for _, values in series), default=0)
        columns = ["Time"] + [name for name, _ in series]
        rows = [
            [str(index)] + [
                values[index] if index < len(values) else None
                for _, values in series
            ]
            for index in range(length)
        ]
        return columns, rows

    def _row_selected(self, _event: tk.Event) -> None:
        selection = self.table.selection()
        if not selection:
            return
        values = self.table.item(selection[0], "values")
        if values:
            time_index = int(values[0])
            self.selected_label.configure(text=f"Selected time step {values[0]}: " + " | ".join(
                f"{column}={value}" for column, value in zip(self.table["columns"][1:], values[1:])
            ))
            if self.on_row_selected:
                self.on_row_selected(time_index)


class OutputWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc, target: TruthTable, prescribed: LogicGraph,
                 solutions: Sequence[LogicGraph] = (),
                 on_resume: Callable[[], None] | None = None,
                 on_close: Callable[[], None] | None = None) -> None:
        super().__init__(master)
        self.title("Pathway solutions")
        self.geometry("900x650")
        self.target, self.prescribed = target, prescribed
        self.solutions = list(solutions)
        self.on_resume = on_resume
        self.on_close = on_close
        self.index = 0
        self.mode = tk.StringVar(value="circle")
        self.waiting_for_resume = False
        self.finished = False
        self.closed = False
        header = ttk.Frame(self, padding=8)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Completed circuit pathways", font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT)
        self.counter = ttk.Label(header, text="")
        self.counter.pack(side=tk.LEFT, padx=18)
        self.resume_button = ttk.Button(header, text="Resume solver", command=self.resume, state=tk.DISABLED)
        self.resume_button.pack(side=tk.RIGHT)
        ttk.Button(header, text="Next", command=lambda: self.change(1)).pack(side=tk.RIGHT)
        ttk.Button(header, text="Previous", command=lambda: self.change(-1)).pack(side=tk.RIGHT, padx=5)
        content = ttk.PanedWindow(self, orient=tk.VERTICAL)
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        tabs = ttk.Notebook(content)
        self.circle_frame = ttk.Frame(tabs)
        self.hierarchy_frame = ttk.Frame(tabs)
        self.diagnostics_frame = SolverDiagnostics(tabs)
        tabs.add(self.circle_frame, text="Circular view")
        tabs.add(self.hierarchy_frame, text="Hierarchy view")
        tabs.add(self.diagnostics_frame, text="Diagnostics")
        self.time_series_frame = TimeSeriesTable(content, self._select_time_series_row)
        content.add(tabs, weight=4)
        content.add(self.time_series_frame, weight=1)
        self.empty_label: ttk.Label | None = None
        if self.solutions:
            self.circle = GraphCanvas(self.circle_frame, self.solutions[0].clone(), editable=False, on_inspect=self.inspect, circular_nodes=True)
            self.hierarchy = GraphCanvas(self.hierarchy_frame, self.solutions[0].clone(), editable=False, on_inspect=self.inspect)
            self.circle.pack(fill=tk.BOTH, expand=True)
            self.hierarchy.pack(fill=tk.BOTH, expand=True)
        else:
            self.empty_label = ttk.Label(self.circle_frame, text="Searching for solutions...")
            self.empty_label.pack(pady=30)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._update()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.on_close:
            self.on_close()
        self.destroy()

    def add_solution(self, solution: LogicGraph) -> None:
        if self.closed or not self.winfo_exists():
            return
        had_solutions = bool(self.solutions)
        self.solutions.append(solution)
        if not had_solutions:
            if self.empty_label:
                self.empty_label.destroy()
                self.empty_label = None
            self.circle = GraphCanvas(self.circle_frame, solution.clone(), editable=False, on_inspect=self.inspect, circular_nodes=True)
            self.hierarchy = GraphCanvas(self.hierarchy_frame, solution.clone(), editable=False, on_inspect=self.inspect)
            self.circle.pack(fill=tk.BOTH, expand=True)
            self.hierarchy.pack(fill=tk.BOTH, expand=True)
        self.index = len(self.solutions) - 1
        self.waiting_for_resume = True
        self.resume_button.configure(state=tk.NORMAL)
        self._update()

    def resume(self) -> None:
        if self.closed or not self.waiting_for_resume:
            return
        self.waiting_for_resume = False
        self.resume_button.configure(state=tk.DISABLED)
        if self.on_resume:
            self.on_resume()

    def mark_finished(self) -> None:
        if self.closed or not self.winfo_exists():
            return
        self.finished = True
        self.waiting_for_resume = False
        self.resume_button.configure(state=tk.DISABLED)
        self._update()

    def change(self, delta: int) -> None:
        if self.solutions:
            self.index = (self.index + delta) % len(self.solutions)
            self._update()

    def _update(self) -> None:
        if self.closed or not self.winfo_exists():
            return
        if not self.solutions:
            self.counter.configure(text="No solutions found" if self.finished else "Searching...")
            #remove 'searching for solutions...' text from canvas
            if self.empty_label:
                self.empty_label.configure(text="No solutions found." if self.finished else "Searching for solutions...")
            return
        graph = self.solutions[self.index]
        suffix = " (solver paused)" if self.waiting_for_resume else ""
        self.counter.configure(text=f"Candidate {self.index + 1} of {len(self.solutions)}{suffix}")
        self.circle.graph = graph.clone()
        self.hierarchy.graph = graph.clone()
        self.circle.views = {node_id: UINode(node) for node_id, node in self.circle.graph.nodes.items()}
        self.hierarchy.views = {node_id: UINode(node) for node_id, node in self.hierarchy.graph.nodes.items()}
        self._arrange_circle(self.circle)
        self._arrange_hierarchy(self.hierarchy)
        self.circle.reset_view()
        self.hierarchy.reset_view()
        self.diagnostics_frame.update_graph(graph)
        self.time_series_frame.update_graph(graph)
        self._select_time_series_row(None)

    def _select_time_series_row(self, time_index: int | None) -> None:
        if not self.solutions:
            return
        for canvas in (self.circle, self.hierarchy):
            labels: dict[str, str] = {}
            value_details: dict[str, str] = {}
            for node_id, node in canvas.graph.nodes.items():
                state_values = []
                for port in node.inputs + node.outputs:
                    if port.values is not None and time_index is not None and time_index < len(port.values):
                        state_values.append(f"{port.name}={port.values[time_index]}")
                if time_index is None:
                    labels[node_id] = canvas.view(node_id).label or node_id
                else:
                    label = canvas.view(node_id).label or node_id
                    labels[node_id] = label
                    value_details[node_id] = (
                        ", ".join(state_values) if state_values else "no propagated value"
                    )
            canvas.set_node_value_labels(labels, value_details if time_index is not None else None)

    @staticmethod
    def _arrange_circle(canvas: GraphCanvas) -> None:
        nodes = [node for node in canvas.graph.nodes.values() if not node.is_imvolution]
        radius_x = max(250, 55 * len(nodes))
        radius_y = max(210, 45 * len(nodes))
        for index, node in enumerate(nodes):
            angle = -math.pi / 2 + 2 * math.pi * index / max(1, len(nodes))
            view = canvas.view(node.node_id)
            view.x, view.y = 420 + radius_x * math.cos(angle), 300 + radius_y * math.sin(angle)
        for node in canvas.graph.nodes.values():
            if not node.is_imvolution:
                continue
            incoming = next((edge for edge in canvas.graph.edges.values()
                             if edge.target_node == node.node_id), None)
            outgoing = next((edge for edge in canvas.graph.edges.values()
                             if edge.source_node == node.node_id), None)
            if incoming and outgoing:
                source = canvas.view(incoming.source_node)
                target = canvas.view(outgoing.target_node)
                view = canvas.view(node.node_id)
                view.x, view.y = (source.x + target.x) / 2, (source.y + target.y) / 2

    @staticmethod
    def _arrange_hierarchy(canvas: GraphCanvas) -> None:
        layers = {node_id: 0 if node.kind == "input" else 1
                  for node_id, node in canvas.graph.nodes.items()}
        for _ in range(max(1, len(canvas.graph.nodes))):
            changed = False
            for edge in canvas.graph.edges.values():
                if edge.source_node not in layers or edge.target_node not in layers:
                    continue
                candidate_layer = layers[edge.source_node] + 1
                if candidate_layer > layers[edge.target_node]:
                    layers[edge.target_node] = candidate_layer
                    changed = True
            if not changed:
                break

        groups: dict[int, list[LogicNode]] = {}
        for node in canvas.graph.nodes.values():
            groups.setdefault(layers.get(node.node_id, 0), []).append(node)
        for layer, nodes in groups.items():
            for index, node in enumerate(nodes):
                view = canvas.view(node.node_id)
                view.x, view.y = 100 + layer * 220, 80 + index * 90

    def inspect(self, title: str, item: object) -> None:
        label = self.circle.view(item.node_id).label if isinstance(item, LogicNode) and item.node_id in self.circle.graph.nodes else None
        show_inspection_dialog(self, title, item, label, self._edge_values(item) if isinstance(item, LogicEdge) else ())

    @staticmethod
    def _format_truth_table(table: TruthTable) -> str:
        lines = ["Truth table:", " | ".join(table.columns),
                 "-+-".join("-" * len(column) for column in table.columns)]
        lines.extend(" | ".join("" if value is None else str(value) for value in row)
                     for row in table.rows)
        return "\n".join(lines)

    def _edge_values(self, edge: LogicEdge) -> tuple[str | None, ...]:
        graph = self.solutions[self.index] if self.solutions else self.prescribed
        source = graph.nodes.get(edge.source_node)
        if source:
            port = next((port for port in source.outputs if port.name == edge.source_port), None)
            if port and port.values is not None:
                return port.values
        return ()


def select_output_target(
    parent: tk.Misc, output_columns: Sequence[str]
) -> tuple[str, str] | None:
    dialog = tk.Toplevel(parent)
    dialog.title("Select solve target")
    dialog.transient(parent)
    dialog.grab_set()
    ttk.Label(dialog, text="Choose the output target to solve for:").pack(anchor=tk.W, padx=12, pady=(12, 6))
    listbox = tk.Listbox(dialog, height=max(3, min(8, len(output_columns))), exportselection=False)
    listbox.pack(fill=tk.BOTH, expand=True, padx=12)
    for column in output_columns:
        listbox.insert(tk.END, column)
    listbox.selection_set(0)
    ttk.Label(dialog, text="Imvolution mode:").pack(
        anchor=tk.W, padx=12, pady=(8, 2)
    )
    mode_var = tk.StringVar(value="free imvolutions")
    ttk.Combobox(
        dialog,
        textvariable=mode_var,
        state="readonly",
        values=("free imvolutions", "bijections", "automorphisms", "no imvolutions"),
    ).pack(fill=tk.X, padx=12)
    result: list[tuple[str, str] | None] = [None]

    def choose() -> None:
        selection = listbox.curselection()
        if selection:
            result[0] = (output_columns[selection[0]], mode_var.get())
        dialog.destroy()

    buttons = ttk.Frame(dialog, padding=12)
    buttons.pack(fill=tk.X)
    ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)
    ttk.Button(buttons, text="Solve", command=choose).pack(side=tk.RIGHT, padx=(0, 6))
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.wait_window()
    return result[0]


class PathwaysApp:
    """Three-phase prescription, solve, and result-display application."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.api_client = ObedienceApiClient(
            os.environ.get("OBEDIENCE_API_URL", "https://obedience-api.vercel.app"),
            api_key=os.environ.get("OBEDIENCE_API_KEY"),
        )
        root.title("Logic pathway designer")
        root.geometry("1250x780")
        root.minsize(900, 560)
        self.target_table = TruthTable(["A", "B", "OUT"], [["0", "0", "0"], ["0", "1", "0"], ["1", "0", "0"], ["1", "1", "1"]])
        self.prescribed_graph = LogicGraph()
        self._gate_number = 0
        self.derived_visibility: dict[str, bool] = {}
        self.palette_buttons: dict[str, ttk.Button] = {}
        self.palette_templates: dict[str, LogicNode] = {}
        self.palette_labels: dict[str, str] = {}
        self.palette_drag_gate: str | None = None
        self.palette_drag_binding_ids: tuple[str, str] | None = None
        self.solution_cursor = uuid.uuid4().hex
        self.solution_window: OutputWindow | None = None
        self.request_in_flight = False
        self._build_ui()

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Pathway designer", font=("Segoe UI", 16, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="Solve", command=self.solve).pack(side=tk.RIGHT)
        ttk.Button(top, text="Reset prescription", command=self.reset).pack(side=tk.RIGHT, padx=6)
        self.status = tk.StringVar(value="Prescription phase: edit the target table and place prescribed gates.")
        ttk.Label(self.root, textvariable=self.status, padding=(8, 0, 8, 8)).pack(anchor=tk.W)
        panes = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        target_frame = ttk.LabelFrame(panes, text="1. Circuit truth table", padding=8)
        panes.add(target_frame, weight=1)
        target_toolbar = ttk.Frame(target_frame)
        target_toolbar.pack(fill=tk.X, pady=(0, 6))
        self.target_editor = TableEditor(
            target_frame, self.target_table, self.sync_terminal_nodes, self.derived_visibility
        )
        self.target_editor.pack(fill=tk.BOTH, expand=True)
        ttk.Button(target_toolbar, text="Import CSV", command=self.target_editor.import_csv).pack(side=tk.LEFT)
        ttk.Button(target_toolbar, text="Save table", command=self.save_circuit_table).pack(side=tk.LEFT, padx=5)
        ttk.Button(target_toolbar, text="Load table", command=self.load_circuit_table).pack(side=tk.LEFT)
        ttk.Button(target_toolbar, text="Randomise table", command=self.randomize_target_table).pack(side=tk.LEFT, padx=5)
        ttk.Button(target_toolbar, text="Add row", command=self.target_editor.add_row).pack(side=tk.LEFT, padx=5)
        ttk.Button(target_toolbar, text="Add input column", command=lambda: self.target_editor.add_column("input")).pack(side=tk.LEFT, padx=5)
        ttk.Button(target_toolbar, text="Add output column", command=lambda: self.target_editor.add_column("output")).pack(side=tk.LEFT)
        ttk.Button(target_toolbar, text="Delete row", command=self.target_editor.delete_row).pack(side=tk.LEFT, padx=5)
        ttk.Button(target_toolbar, text="Delete column", command=self.target_editor.delete_column).pack(side=tk.LEFT)
        self.derived_controls = ttk.Frame(target_frame)
        self.derived_controls.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(self.derived_controls, text="Derived gate outputs:").pack(side=tk.LEFT)

        middle = ttk.LabelFrame(panes, text="2. Prescribed gates", padding=8)
        panes.add(middle, weight=2)
        self.editor_canvas = GraphCanvas(
            middle, self.prescribed_graph, on_inspect=self.inspect,
            on_message=self.status.set,
            on_graph_change=self.sync_terminal_nodes,
        )
        self.editor_canvas.pack(fill=tk.BOTH, expand=True)
        palette = ttk.Frame(middle)
        palette.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(palette, text="Add gate +", command=self.add_gate_dialog).pack(side=tk.LEFT)
        ttk.Button(palette, text="Save gate menu", command=self.save_gate_menu).pack(side=tk.LEFT, padx=5)
        ttk.Button(palette, text="Load gate menu", command=self.load_gate_menu).pack(side=tk.LEFT)
        ttk.Button(palette, text="Reset view", command=self.editor_canvas.reset_view).pack(side=tk.RIGHT)
        template_bar = ttk.Frame(middle)
        template_bar.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(template_bar, text="Drag a gate template:").pack(side=tk.LEFT, padx=(0, 4))
        self.gate_palette_canvas = tk.Canvas(template_bar, height=42, highlightthickness=0)
        self.gate_palette_canvas.pack(side=tk.TOP, fill=tk.X, expand=True)
        template_scroll = ttk.Scrollbar(template_bar, orient=tk.HORIZONTAL,
                                        command=self.gate_palette_canvas.xview)
        template_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.gate_palette_canvas.configure(xscrollcommand=template_scroll.set)
        self.gate_palette = ttk.Frame(self.gate_palette_canvas)
        self.gate_palette_canvas.create_window((0, 0), window=self.gate_palette, anchor=tk.NW)
        self.gate_palette.bind(
            "<Configure>",
            lambda _event: self.gate_palette_canvas.configure(
                scrollregion=self.gate_palette_canvas.bbox("all")
            ),
        )
        self.sync_terminal_nodes()

    def save_circuit_table(self) -> None:
        filename = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Truth tables", "*.json"), ("All files", "*")],
        )
        if not filename:
            return
        table = self.target_editor.get_table()
        payload = {
            "columns": table.columns,
            "column_kinds": table.column_kinds,
            "rows": table.rows,
            "parameter_domains": table.parameter_domains,
            "parameter_positions": table.parameter_positions,
        }
        try:
            with open(filename, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
            self.status.set("Saved circuit truth table.")
        except OSError as exc:
            messagebox.showerror("Save table failed", str(exc), parent=self.root)

    def load_circuit_table(self) -> None:
        filename = filedialog.askopenfilename(
            filetypes=[("Truth tables", "*.json"), ("All files", "*")],
        )
        if not filename:
            return
        try:
            with open(filename, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
            table = TruthTable(
                payload["columns"], payload["rows"], payload["column_kinds"],
                {
                    str(column): tuple(str(value) for value in values)
                    for column, values in payload.get("parameter_domains", {}).items()
                },
                {
                    str(column): int(position)
                    for column, position in payload.get("parameter_positions", {}).items()
                },
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("Load table failed", str(exc), parent=self.root)
            return
        self.target_editor.table = table
        self.target_editor.refresh()
        self.sync_terminal_nodes()
        self.status.set("Loaded circuit truth table.")

    def randomize_target_table(self) -> None:
        radix = simpledialog.askinteger(
            "Randomise truth table", "Number system radix (2-16):",
            initialvalue=2, minvalue=2, maxvalue=16, parent=self.root,
        )
        if radix is None:
            return
        self.target_editor.table = self.target_editor.table.randomized(radix)
        self.target_editor.refresh()
        self.sync_terminal_nodes()

    def register_gate_palette(self, node_id: str, node: LogicNode | None = None,
                              label: str | None = None) -> None:
        node = node or self.prescribed_graph.nodes[node_id]
        label = label or node_id
        button = ttk.Button(self.gate_palette, text=label, width=max(8, len(label) + 3))
        button.pack(side=tk.LEFT, padx=2)
        button.bind("<ButtonPress-1>", lambda event: self._start_palette_drag(node_id, event))
        button.bind("<Button-3>", lambda event: self._show_gate_template_menu(event, node_id))
        self.palette_buttons[node_id] = button
        self.palette_templates[node_id] = self._copy_gate_template(node)
        self.palette_labels[node_id] = label

    @staticmethod
    def _template_to_dict(node: LogicNode, label: str) -> dict[str, object]:
        table = node.truth_table
        return {
            "node_id": node.node_id,
            "label": label,
            "columns": table.columns if table else [],
            "column_kinds": table.column_kinds if table else [],
            "rows": table.rows if table else [],
            "parameter_domains": table.parameter_domains if table else {},
            "parameter_positions": table.parameter_positions if table else {},
            "inputs": [
                {"name": port.name, "column": port.column, "values": list(port.values or ())}
                for port in node.inputs
            ],
            "outputs": [
                {"name": port.name, "column": port.column, "values": list(port.values or ())}
                for port in node.outputs
            ],
        }

    def save_gate_menu(self) -> None:
        filename = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("Gate menus", "*.json"), ("All files", "*")],
        )
        if not filename:
            return
        payload = [self._template_to_dict(node, self.palette_labels.get(node_id, node_id))
                   for node_id, node in self.palette_templates.items()]
        try:
            with open(filename, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
            self.status.set(f"Saved {len(payload)} gate template(s).")
        except OSError as exc:
            messagebox.showerror("Save gate menu failed", str(exc), parent=self.root)

    def load_gate_menu(self) -> None:
        filename = filedialog.askopenfilename(
            filetypes=[("Gate menus", "*.json"), ("All files", "*")],
        )
        if not filename:
            return
        try:
            with open(filename, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
            if not isinstance(payload, list):
                raise ValueError("A gate menu file must contain a JSON list.")
            templates: dict[str, LogicNode] = {}
            labels: dict[str, str] = {}
            for item in payload:
                if not isinstance(item, dict):
                    raise ValueError("Each gate template must be a JSON object.")
                node_id = str(item["node_id"])
                columns = [str(column) for column in item["columns"]]
                kinds = [str(kind) for kind in item["column_kinds"]]
                table = TruthTable(
                    columns, item["rows"], kinds,
                    {
                        str(column): tuple(str(value) for value in values)
                        for column, values in item.get("parameter_domains", {}).items()
                    },
                    {
                        str(column): int(position)
                        for column, position in item.get("parameter_positions", {}).items()
                    },
                )
                inputs = [LogicPort(str(port["name"]), "input", port.get("column"),
                                    tuple(port.get("values") or ()))
                          for port in item.get("inputs", [])]
                outputs = [LogicPort(str(port["name"]), "output", port.get("column"),
                                     tuple(port.get("values") or ()))
                           for port in item.get("outputs", [])]
                templates[node_id] = LogicNode(node_id, "gate", table, inputs, outputs)
                labels[node_id] = str(item.get("label", node_id))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("Load gate menu failed", str(exc), parent=self.root)
            return

        for button in self.palette_buttons.values():
            button.destroy()
        self.palette_buttons.clear()
        self.palette_templates.clear()
        self.palette_labels.clear()
        for node_id, node in templates.items():
            self.register_gate_palette(node_id, node, labels[node_id])
        self.status.set(f"Loaded {len(templates)} gate template(s).")

    def _show_gate_template_menu(self, event: tk.Event, node_id: str) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Edit gate", command=lambda: self.edit_gate_template(node_id))
        menu.add_command(label="Delete gate", command=lambda: self.delete_gate_template(node_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def delete_gate_template(self, node_id: str) -> None:
        button = self.palette_buttons.pop(node_id, None)
        if button is not None:
            button.destroy()
        self.palette_templates.pop(node_id, None)
        self.palette_labels.pop(node_id, None)
        self.status.set("Deleted gate template; existing canvas gates were preserved.")

    def edit_gate_template(self, node_id: str) -> None:
        if node_id not in self.palette_templates:
            return
        self._open_gate_dialog(node_id)

    def _start_palette_drag(self, node_id: str, _event: tk.Event) -> None:
        self.palette_drag_gate = node_id
        motion_id = self.root.bind("<Motion>", self._palette_motion, add="+")
        release_id = self.root.bind("<ButtonRelease-1>", self._finish_palette_drag, add="+")
        self.palette_drag_binding_ids = (motion_id, release_id)

    def _palette_motion(self, event: tk.Event) -> None:
        if not self.palette_drag_gate:
            return
        canvas = self.editor_canvas.canvas
        x = event.x_root - canvas.winfo_rootx()
        y = event.y_root - canvas.winfo_rooty()
        self.editor_canvas.draw()
        canvas.create_rectangle(x - 58, y - 30, x + 58, y + 30,
                                outline="#0078d7", dash=(5, 3), width=2)

    def _finish_palette_drag(self, event: tk.Event) -> None:
        if not self.palette_drag_gate:
            return
        node_id = self.palette_drag_gate
        canvas = self.editor_canvas.canvas
        x = event.x_root - canvas.winfo_rootx()
        y = event.y_root - canvas.winfo_rooty()
        self._clear_palette_drag()
        if not (0 <= x <= canvas.winfo_width() and 0 <= y <= canvas.winfo_height()):
            self.editor_canvas.draw()
            return
        world_x, world_y = self.editor_canvas.screen_to_world(x, y)
        self._duplicate_gate(node_id, world_x, world_y)

    def _clear_palette_drag(self) -> None:
        if self.palette_drag_binding_ids:
            motion_id, release_id = self.palette_drag_binding_ids
            self.root.unbind("<Motion>", motion_id)
            self.root.unbind("<ButtonRelease-1>", release_id)
        self.palette_drag_binding_ids = None
        self.palette_drag_gate = None

    def _duplicate_gate(self, source_id: str, x: float, y: float) -> None:
        source = self.palette_templates.get(source_id)
        if not source:
            return
        self._gate_number += 1
        node = LogicGraph.generate_node(
            "gate", 
            source.truth_table.copy() if source.truth_table else None,
            [LogicPort(port.name, port.direction, port.column, port.values) for port in source.inputs],
            [LogicPort(port.name, port.direction, port.column, port.values) for port in source.outputs],
        )
        self.prescribed_graph.add_node(node)
        self.editor_canvas.views[node.node_id] = UINode(node, x=x, y=y,
                               label=self.palette_labels.get(source_id, node.node_id))
        self.editor_canvas.selected_node = node.node_id
        self.editor_canvas.draw()

    @staticmethod
    def _copy_gate_template(source: LogicNode) -> LogicNode:
        return LogicNode(
            source.node_id,
            source.kind,
            source.truth_table.copy() if source.truth_table else None,
            inputs=[LogicPort(port.name, port.direction, port.column, port.values) for port in source.inputs],
            outputs=[LogicPort(port.name, port.direction, port.column, port.values) for port in source.outputs],
        )

    def sync_terminal_nodes(self) -> None:
        """Make circuit-table columns the only source of terminal graph nodes."""
        table = self.target_editor.get_table()
        self._sync_derived_columns(table)
        self.target_editor.table = table
        expected: dict[str, tuple[str, str]] = {}
        for kind, prefix in (("input", "input"), ("output", "output")):
            for index, column in enumerate(table.columns):
                if table.column_kinds[index] == kind:
                    expected[f"{prefix}:{column}"] = (kind, column)

        for node_id in list(self.prescribed_graph.nodes):
            node = self.prescribed_graph.nodes[node_id]
            if node.kind in {"input", "output"} and node_id not in expected:
                self.prescribed_graph.remove_node(node_id)

        input_index = output_index = 0
        for node_id, (kind, column) in expected.items():
            column_index = table.columns.index(column)
            values = tuple(row[column_index] for row in table.rows)
            if kind == "input":
                ports = ([], [LogicPort(column, "output", column, values)])
                input_index += 1
                x, y = 110, 80 + 75 * (input_index - 1)
            else:
                ports = ([LogicPort(column, "input", column, values)], [])
                output_index += 1
                x, y = 560, 80 + 75 * (output_index - 1)
            if node_id in self.prescribed_graph.nodes:
                node = self.prescribed_graph.nodes[node_id]
                node.inputs, node.outputs = ports
                view = self.editor_canvas.view(node_id)
                view.x, view.y, view.label = x, y, column
            else:
                self.prescribed_graph.add_node(LogicNode(node_id, kind,
                                                         inputs=ports[0], outputs=ports[1]))
                self.editor_canvas.views[node_id] = UINode(
                    self.prescribed_graph.nodes[node_id], x=x, y=y, label=column
                )
        self._propagate_derived_values(table)
        self.target_editor.table = table
        self.target_editor.refresh()
        self._refresh_derived_controls()
        self.editor_canvas.draw()

    def _sync_derived_columns(self, table: TruthTable) -> None:
        specs = {
            port.column: node_id
            for node_id, node in self.prescribed_graph.nodes.items()
            if node.kind == "gate"
            for port in node.outputs
            if port.column
        }
        old_values = {
            column: table.rows[row_index][column_index]
            for column_index, column in enumerate(table.columns)
            if table.column_kinds[column_index] == "derived"
            for row_index in range(len(table.rows))
        }
        keep = [index for index, kind in enumerate(table.column_kinds)
                if kind != "derived" or table.columns[index] in specs]
        table.columns = [table.columns[index] for index in keep]
        table.column_kinds = [table.column_kinds[index] for index in keep]
        table.rows = [[row[index] for index in keep] for row in table.rows]
        for column in specs:
            if column in table.columns:
                continue
            table.columns.append(column)
            table.column_kinds.append("derived")
            for row_index, row in enumerate(table.rows):
                row.append(old_values.get(column) if old_values.get(column) is not None else None)
        table.sort_columns()

    def _refresh_derived_controls(self) -> None:
        for child in self.derived_controls.winfo_children()[1:]:
            child.destroy()
        for index, column in enumerate(self.target_editor.table.columns):
            if self.target_editor.table.column_kinds[index] != "derived":
                continue
            variable = tk.BooleanVar(value=self.derived_visibility.get(column, True))
            self.derived_visibility[column] = variable.get()
            ttk.Checkbutton(
                self.derived_controls,
                text=column,
                variable=variable,
                command=lambda name=column, value=variable: self.target_editor.set_derived_visibility(name, value.get()),
            ).pack(side=tk.LEFT, padx=4)

    def _propagate_derived_values(self, table: TruthTable) -> None:
        for index, kind in enumerate(table.column_kinds):
            if kind == "derived":
                for row in table.rows:
                    row[index] = None
        for node in self.prescribed_graph.nodes.values():
            if node.kind != "input" or not node.outputs:
                continue
            port = node.outputs[0]
            column_index = table.columns.index(port.column) if port.column in table.columns else -1
            port.values = tuple(row[column_index] for row in table.rows) if column_index >= 0 else None

        unresolved = set(node.node_id for node in self.prescribed_graph.nodes.values() if node.kind == "gate")
        while unresolved:
            progress = False
            for node_id in list(unresolved):
                node = self.prescribed_graph.nodes[node_id]
                incoming = {edge.target_port: edge for edge in self.prescribed_graph.edges.values()
                            if edge.target_node == node_id}
                if any(port.name not in incoming for port in node.inputs):
                    continue
                input_values: list[tuple[str | None, ...]] = []
                complete = True
                for port in node.inputs:
                    edge = incoming[port.name]
                    source = self.prescribed_graph.nodes.get(edge.source_node)
                    source_port = next((item for item in source.outputs if item.name == edge.source_port), None) if source else None
                    if not source_port or source_port.values is None:
                        complete = False
                        break
                    input_values.append(source_port.values)
                if not complete:
                    continue
                output_values: list[str | None] = []
                input_columns = [port.column for port in node.inputs]
                output_column = node.outputs[0].column if node.outputs else None
                if not node.truth_table or not output_column:
                    continue
                table_input_columns = [
                    column for column in input_columns
                    if column in node.truth_table.columns
                ]
                for row_index in range(len(table.rows)):
                    arguments = tuple(values[row_index] for values in input_values)
                    result = None
                    domain_violation = any(
                        node.truth_table.parameter_domains.get(column)
                        and arguments[input_columns.index(column)]
                        not in node.truth_table.parameter_domains[column]
                        for column in input_columns
                        if column in node.truth_table.parameter_domains
                    )
                    if domain_violation:
                        output_values.append(None)
                        continue
                    for gate_row in node.truth_table.rows:
                        table_arguments = tuple(
                            gate_row[node.truth_table.columns.index(column)]
                            for column in table_input_columns
                        )
                        argument_subset = tuple(
                            arguments[input_columns.index(column)]
                            for column in table_input_columns
                        )
                        if table_arguments == argument_subset:
                            result = gate_row[node.truth_table.columns.index(output_column)] if output_column in node.truth_table.columns else gate_row[-1]
                            break
                    result = render_output_template(result, input_columns, arguments)
                    output_values.append(result)
                node.outputs[0].values = tuple(output_values)
                column_index = table.columns.index(output_column)
                for row_index, value in enumerate(output_values):
                    table.rows[row_index][column_index] = value
                unresolved.remove(node_id)
                progress = True
            if not progress:
                break

    def add_gate_dialog(self) -> None:
        self._open_gate_dialog()

    def _open_gate_dialog(self, template_id: str | None = None) -> None:
        dialog = tk.Toplevel(self.root)
        editing = template_id is not None
        dialog.title("Edit gate" if editing else "Create prescribed gate")
        dialog.transient(self.root)
        form = ttk.Frame(dialog, padding=14)
        form.pack(fill=tk.BOTH, expand=True)
        template = self.palette_templates.get(template_id) if template_id else None
        template_label = self.palette_labels.get(template_id, "AND") if template_id else "AND"
        name_var = tk.StringVar(value=template_label)
        inputs_var = tk.StringVar(value=str(len(template.inputs)) if template else "2")
        ttk.Label(form, text="Gate name").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=name_var, width=24).grid(row=0, column=1, pady=4)
        ttk.Label(form, text="Input ports").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(form, from_=1, to=8, textvariable=inputs_var, width=6).grid(row=1, column=1, sticky="w", pady=4)
        table_area = ttk.Frame(form)
        table_area.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=8)
        table_area.columnconfigure(1, weight=1)
        table_area.rowconfigure(0, weight=1)
        domain_frame = ttk.LabelFrame(table_area, text="Parameter domains", padding=6)
        domain_frame.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        table_holder = ttk.LabelFrame(table_area, text="Gate truth table", padding=6)
        table_holder.grid(row=0, column=1, sticky="nsew")
        if template and template.truth_table:
            initial_table = template.truth_table.copy()
            initial_domains = initial_table.parameter_domains.copy()
            domain_positions = initial_table.parameter_positions.copy()
            initial_table.parameter_domains = {}
            initial_table.parameter_positions = {}
        else:
            initial_domains = {}
            domain_positions = {}
            target_table = self.target_editor.get_table()
            target_inputs = [column for index, column in enumerate(target_table.columns)
                             if target_table.column_kinds[index] == "input"]
            target_outputs = [column for index, column in enumerate(target_table.columns)
                              if target_table.column_kinds[index] == "output"]
            gate_input_columns = target_inputs[:2]
            while len(gate_input_columns) < 2:
                fallback = f"in{len(gate_input_columns) + 1}"
                if fallback not in gate_input_columns:
                    gate_input_columns.append(fallback)
            gate_columns = gate_input_columns + (target_outputs[:1] or ["out"])
            gate_kinds = ["input"] * (len(gate_columns) - 1) + ["output"]
            #initial columns are 010101..., 0011001100..., 0000111100001111..., etc. for each input, and the output is 0,0,0,1 for AND
            initial_rows = []
            num_rows = 2 ** (len(gate_columns) - 1)
            for i in range(num_rows):
                row = []
                for j in range(len(gate_columns) - 1):
                    row.append(str((i // (2 ** j)) % 2))
                row.append("0")# if i < num_rows // 2 else "1")
                initial_rows.append(row)
            initial_table = TruthTable(
                gate_columns,
                initial_rows,
                gate_kinds,
            )
        gate_editor = TableEditor(
            table_holder,
            initial_table,
        )
        gate_editor.pack(fill=tk.BOTH, expand=True)
        domain_editor = ParameterDomainEditor(domain_frame)
        domain_editor.pack(fill=tk.X)
        domain_editor.set_domains(list(initial_domains), initial_domains)
        ttk.Label(
            form,
            text="◇ columns referenced by an output f-string are parameter domains; blank entries mean any state.",
            foreground="#777777",
        ).grid(row=3, column=0, columnspan=2, sticky="w")
        gate_toolbar = ttk.Frame(form)
        gate_toolbar.grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Button(gate_toolbar, text="Import CSV", command=gate_editor.import_csv).pack(side=tk.LEFT)
        ttk.Button(
            gate_toolbar, text="Randomise table",
            command=lambda: self.randomize_gate_table(gate_editor, dialog),
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(gate_toolbar, text="Add input column", command=lambda: gate_editor.add_column("input")).pack(side=tk.LEFT, padx=5)
        ttk.Button(gate_toolbar, text="Add output column", command=lambda: gate_editor.add_column("output")).pack(side=tk.LEFT)
        ttk.Button(gate_toolbar, text="Add row", command=gate_editor.add_row).pack(side=tk.LEFT, padx=5)
        ttk.Button(gate_toolbar, text="Delete row", command=gate_editor.delete_row).pack(side=tk.LEFT, padx=5)
        ttk.Button(gate_toolbar, text="Delete column", command=gate_editor.delete_column).pack(side=tk.LEFT)
        ttk.Label(
            form,
            text='Output templates: f"state={Input A}/{Input B}"  (use {Input Column} placeholders)',
            foreground="#53635a",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def sync_parameter_domains() -> None:
            table = gate_editor.get_table()
            known_columns = list(dict.fromkeys(
                table.columns + list(domain_editor.get_domains())
            ))
            output_indices = [
                index for index, kind in enumerate(table.column_kinds)
                if kind == "output"
            ]
            referenced = [
                column
                for row in table.rows
                for output_index in output_indices
                for column in template_columns(
                    row[output_index] if output_index < len(row) else None,
                    known_columns,
                )
            ]
            referenced = list(dict.fromkeys(referenced))
            current_domains = domain_editor.get_domains()
            current_parameter_columns = set(current_domains)

            for column in referenced:
                if column in table.columns:
                    domain_positions.setdefault(column, table.columns.index(column))
                    column_index = table.columns.index(column)
                    values = tuple(
                        row[column_index]
                        for row in table.rows
                        if row[column_index] not in (None, "")
                    )
                    if column not in current_domains:
                        current_domains[column] = values

            for column in referenced:
                if column not in table.columns:
                    continue
                column_index = table.columns.index(column)
                del table.columns[column_index]
                del table.column_kinds[column_index]
                for row in table.rows:
                    del row[column_index]

            for column in current_parameter_columns - set(referenced):
                if column in table.columns:
                    continue
                position = min(domain_positions.get(column, len(table.columns)), len(table.columns))
                table.columns.insert(position, column)
                table.column_kinds.insert(position, "input")
                domain_values = current_domains.get(column, ())
                for row_index, row in enumerate(table.rows):
                    row.insert(
                        position,
                        domain_values[row_index] if row_index < len(domain_values) else None,
                    )
                current_domains.pop(column, None)

            gate_editor.table = table
            gate_editor.refresh()
            domain_editor.set_domains(referenced, current_domains)

        gate_editor.on_change = sync_parameter_domains
        editing_template_id = template_id

        def create() -> None:
            try:
                count = max(1, int(inputs_var.get()))
            except ValueError:
                messagebox.showerror("Invalid gate", "Input ports must be a number.", parent=dialog)
                return
            table = gate_editor.get_table()
            input_indices = [
                index for index, kind in enumerate(table.column_kinds)
                if kind == "input"
            ]
            input_columns = [table.columns[index] for index in input_indices]
            all_input_columns = list(dict.fromkeys(input_columns + list(domain_editor.get_domains())))
            if table.columns and table.rows:
                try:
                    for row in table.rows:
                        render_output_template(
                            row[-1],
                            all_input_columns,
                            [
                                row[table.columns.index(column)]
                                if column in table.columns else None
                                for column in all_input_columns
                            ],
                        )
                except ValueError as error:
                    messagebox.showerror("Invalid output template", str(error), parent=dialog)
                    return
            referenced_columns = tuple(dict.fromkeys(
                column
                for row in table.rows
                for output_index, kind in enumerate(table.column_kinds)
                if kind == "output"
                for column in template_columns(
                    row[output_index] if output_index < len(row) else None,
                    all_input_columns,
                )
            ))
            missing_columns = [column for column in referenced_columns if column not in all_input_columns]
            if missing_columns:
                messagebox.showerror(
                    "Invalid output template",
                    "Output templates can reference input columns only: "
                    + ", ".join(missing_columns),
                    parent=dialog,
                )
                return
            domain_columns = [column for column in all_input_columns if column in referenced_columns]
            parameter_positions = {
                column: domain_positions.get(
                    column,
                    table.columns.index(column) if column in table.columns else len(table.columns),
                )
                for column in domain_columns
            }
            parameter_domains = domain_editor.get_domains()
            if domain_columns:
                remove_indices = {
                    table.columns.index(column) for column in domain_columns
                    if column in table.columns
                }
                table.columns = [
                    column for index, column in enumerate(table.columns)
                    if index not in remove_indices
                ]
                table.column_kinds = [
                    kind for index, kind in enumerate(table.column_kinds)
                    if index not in remove_indices
                ]
                table.rows = [
                    [value for index, value in enumerate(row) if index not in remove_indices]
                    for row in table.rows
                ]
                table.parameter_domains.update(parameter_domains)
                table.parameter_positions.update(parameter_positions)
                input_columns = [
                    column for column in input_columns if column not in domain_columns
                ]
                parameter_domains = {
                    column: parameter_domains.get(column, ())
                    for column in domain_columns
                }
                input_columns.extend(domain_columns)
            if count < len(all_input_columns):
                messagebox.showerror(
                    "Invalid gate",
                    f"This gate needs at least {len(all_input_columns)} input ports.",
                    parent=dialog,
                )
                return
            existing_output = template.outputs[0].column if template and template.outputs else None
            output_column = existing_output or self._unique_gate_output_name(name_var.get().strip() or "gate")
            if table.columns:
                table.columns[-1] = output_column
            inputs = []
            for index in range(count):
                column = input_columns[index] if index < len(input_columns) else None
                values = (
                    tuple(row[table.columns.index(column)] for row in table.rows)
                    if column in table.columns else None
                )
                inputs.append(LogicPort(column, "input", column, values))
            output_column = table.columns[-1] if table.columns else None
            output_values = tuple(row[-1] for row in table.rows) if output_column else None
            new_template_id = editing_template_id or f"gate_template_{self._gate_number + 1}"
            updated_template = LogicNode(
                new_template_id, "gate", table,
                inputs, [LogicPort(output_column, "output", output_column, output_values)]
            )
            label = name_var.get().strip() or updated_template.node_id
            if editing and editing_template_id:
                self.palette_templates[editing_template_id] = updated_template
                self.palette_labels[editing_template_id] = label
                button = self.palette_buttons[editing_template_id]
                button.configure(text=label, width=max(8, len(label) + 3))
                original = self.prescribed_graph.nodes.get(editing_template_id)
                if original:
                    original.truth_table = table
                    original.inputs = inputs
                    original.outputs = updated_template.outputs
                    self.editor_canvas.view(editing_template_id).label = label
            else:
                self._gate_number += 1
                new_template_id = f"gate_template_{self._gate_number}"
                updated_template.node_id = new_template_id
                self.register_gate_palette(new_template_id, updated_template, label)
            self.sync_terminal_nodes()
            dialog.destroy()

        ttk.Button(form, text="Save gate" if editing else "Create gate", command=create).grid(row=6, column=1, sticky="e")
        form.columnconfigure(1, weight=1)
        form.rowconfigure(2, weight=1)
        dialog.geometry("560x430")

    def randomize_gate_table(self, editor: TableEditor, parent: tk.Misc) -> None:
        radix = simpledialog.askinteger(
            "Randomise gate truth table", "Number system radix (2-16):",
            initialvalue=2, minvalue=2, maxvalue=16, parent=parent,
        )
        if radix is None:
            return
        editor.table = editor.table.randomized(radix)
        editor.refresh()

    def _unique_gate_output_name(self, label: str) -> str:
        base = f"{label}_out"
        used = set(self.target_editor.table.columns)
        used.update(
            port.column
            for node in self.prescribed_graph.nodes.values()
            for port in node.outputs
            if port.column
        )
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}{suffix}"
            suffix += 1
        return candidate

    def inspect(self, title: str, item: object) -> None:
        label = self.editor_canvas.view(item.node_id).label if isinstance(item, LogicNode) else None
        show_inspection_dialog(
            self.root,
            title,
            item,
            label,
            self._edge_values(item) if isinstance(item, LogicEdge) else (),
        )

    @staticmethod
    def _format_truth_table(table: TruthTable) -> str:
        lines = ["Truth table:", " | ".join(table.columns), "-+-".join("-" * len(column) for column in table.columns)]
        lines.extend(" | ".join("" if value is None else str(value) for value in row) for row in table.rows)
        return "\n".join(lines)

    def _edge_values(self, edge: LogicEdge) -> tuple[str | None, ...]:
        source = self.prescribed_graph.nodes.get(edge.source_node)
        if source:
            port = next((port for port in source.outputs if port.name == edge.source_port), None)
            if port and port.values is not None:
                return port.values
        return ()

    def _poll_solver_results(
        self,
        result_queue: queue.Queue[tuple[str, object | None]],
        window: OutputWindow,
        target_name: str,
    ) -> None:
        if window.closed or not window.winfo_exists():
            return
        try:
            while True:
                event, payload = result_queue.get_nowait()
                if event == "solution" and isinstance(payload, LogicGraph):
                    window.add_solution(payload)
                    self.status.set(f"Found a scenario for {target_name}; press Resume solver to continue.")
                elif event == "done":
                    self.request_in_flight = False
                    window.mark_finished()
                    self.status.set(f"Found the next solution for {target_name}.")
                    return
                elif event == "error":
                    self.request_in_flight = False
                    window.mark_finished()
                    messagebox.showerror("Solve failed", str(payload), parent=self.root)
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._poll_solver_results, result_queue, window, target_name)


    @staticmethod
    def _table_payload(table: TruthTable) -> dict[str, object]:
        return {
            "columns": table.columns.copy(),
            "column_kinds": table.column_kinds.copy(),
            "rows": [row.copy() for row in table.rows],
            "parameter_domains": {
                column: list(values)
                for column, values in table.parameter_domains.items()
            },
            "parameter_positions": table.parameter_positions.copy(),
        }

    def _standard_parts_payload(self) -> dict[str, dict[str, object]]:
        return {
            self.palette_labels.get(node_id, node_id): self._table_payload(node.truth_table)
            for node_id, node in self.palette_templates.items()
            if node.truth_table is not None
        }

    @staticmethod
    def _graph_from_api_payload(
        payload: dict[str, object],
        circuit: TruthTable,
    ) -> LogicGraph:
        graph = LogicGraph(
            pairing_loss=[float(value) for value in payload.get("pairing_loss", [])],
            pairing_labels=[str(value) for value in payload.get("pairing_labels", [])],
        )
        circuit_values = {
            column: tuple(row[index] for row in circuit.rows)
            for index, column in enumerate(circuit.columns)
        }
        for item in payload.get("nodes", []):
            if not isinstance(item, dict):
                continue
            table_payload = item.get("truth_table")
            table = None
            if isinstance(table_payload, dict):
                table = TruthTable(
                    [str(column) for column in table_payload["columns"]],
                    table_payload["rows"],
                    [str(kind) for kind in table_payload["column_kinds"]],
                )
            inputs = [
                LogicPort(str(name), "input", str(name), circuit_values.get(str(name)))
                for name in item.get("inputs", [])
            ]
            outputs = [
                LogicPort(str(name), "output", str(name), circuit_values.get(str(name)))
                for name in item.get("outputs", [])
            ]
            if table is not None:
                input_columns = [
                    column for column, kind in zip(table.columns, table.column_kinds)
                    if kind == "input"
                ]
                output_columns = [
                    column for column, kind in zip(table.columns, table.column_kinds)
                    if kind == "output"
                ]
                for port, column in zip(inputs, input_columns):
                    port.column = column
                    port.values = tuple(row[table.columns.index(column)] for row in table.rows)
                for port, column in zip(outputs, output_columns):
                    port.column = column
                    port.values = tuple(row[table.columns.index(column)] for row in table.rows)
            graph.add_node(LogicNode(
                str(item["id"]),
                str(item["kind"]),
                table,
                inputs,
                outputs,
                str(item["id"]).startswith("imv_"),
            ))
        for item in payload.get("edges", []):
            if isinstance(item, dict):
                edge = LogicEdge(
                    str(item["edge_id"]),
                    str(item["source_node"]),
                    str(item["source_port"]),
                    str(item["target_node"]),
                    str(item["target_port"]),
                    item.get("column"),
                )
                graph.edges[edge.edge_id] = edge
        return graph

    def solve(self) -> None:
        if self.request_in_flight:
            return
        if not self.api_client.api_key:
            messagebox.showerror(
                "Solve failed",
                "Set OBEDIENCE_API_KEY before starting the application.",
                parent=self.root,
            )
            return
        target = self.target_editor.get_table()
        output_columns = [
            column for column, kind in zip(target.columns, target.column_kinds)
            if kind == "output"
        ]
        if not output_columns:
            messagebox.showerror(
                "Solve failed",
                "Add at least one output column before solving.",
                parent=self.root,
            )
            return
        selection = select_output_target(self.root, output_columns)
        if selection is None:
            return
        selected_target, imvolution_mode = selection
        selected_columns = [
            (index, column, kind)
            for index, (column, kind) in enumerate(
                zip(target.columns, target.column_kinds)
            )
            if kind == "input" or column == selected_target
        ]
        solver_target = TruthTable(
            [column for _, column, _ in selected_columns],
            [[row[index] for index, _, _ in selected_columns] for row in target.rows],
            [kind for _, _, kind in selected_columns],
        )
        result_queue: queue.Queue[tuple[str, object | None]] = queue.Queue()
        stop_event = threading.Event()
        window = self.solution_window
        if window is None or window.closed or not window.winfo_exists():
            window = OutputWindow(
                self.root,
                target,
                self.prescribed_graph.clone(),
                on_resume=lambda: None,
                on_close=stop_event.set,
            )
            self.solution_window = window
        self.request_in_flight = True
        self.status.set(
            f"Solving target {selected_target} with {imvolution_mode}; "
            "waiting for the Obedience API."
        )
        worker = threading.Thread(
            target=self._run_api_solver,
            args=(solver_target, self._standard_parts_payload(), result_queue,
                  stop_event, imvolution_mode),
            daemon=True,
        )
        worker.start()
        self._poll_solver_results(result_queue, window, selected_target)

    def _run_api_solver(
        self,
        target: TruthTable,
        standard_parts: dict[str, dict[str, object]],
        result_queue: queue.Queue[tuple[str, object | None]],
        stop_event: threading.Event,
        imvolution_mode: str,
    ) -> None:
        try:
            job_id = self.api_client.submit_job({
                "operation": "solve",
                "circuit": self._table_payload(target),
                "standard_parts": standard_parts,
                "imvolution_mode": imvolution_mode,
                "iteration_limit": 1000,
                "solution_limit": 1,
                "solution_cursor": self.solution_cursor,
            })
            status = self.api_client.wait_for_job(job_id)
            if stop_event.is_set():
                return
            if status.error:
                raise RuntimeError(status.error)
            solutions = (status.result or {}).get("solutions", [])
            for solution in solutions:
                if stop_event.is_set():
                    return
                result_queue.put(("solution", self._graph_from_api_payload(solution, target)))
            result_queue.put(("done", None))
        except (ObedienceApiError, TimeoutError, ValueError, RuntimeError) as exc:
            result_queue.put(("error", f"{type(exc).__name__}: {exc}"))

    def reset(self) -> None:
        self.prescribed_graph = LogicGraph()
        self.editor_canvas.graph = self.prescribed_graph
        self.editor_canvas.views = {}
        self.sync_terminal_nodes()
        self.solution_cursor = uuid.uuid4().hex
        if self.solution_window is not None and not self.solution_window.closed:
            self.solution_window.close()
        self.solution_window = None
        self.status.set("Prescription phase: edit the target table and place prescribed gates.")


def main() -> None:
    root = tk.Tk()
    PathwaysApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()