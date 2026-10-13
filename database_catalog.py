"""將提供的 Markdown 科目聯集轉為可核對的資料庫能力清單。"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from models import AccountingItem

STATEMENTS = ("資產負債表", "綜合損益表", "現金流量表", "權益變動表")
HEADER = re.compile(r"### (資產負債表|綜合損益表|現金流量表|權益變動表)（(\d+) 個 QName）")


@dataclass(frozen=True)
class DatabaseCatalog:
    source_name: str
    source_sha256: str
    source_scope: str
    items: tuple[AccountingItem, ...]

    @property
    def by_id(self) -> dict[str, AccountingItem]:
        return {item.item_id: item for item in self.items}

    def to_state(self) -> dict:
        """保留完整 QName，命名空間只傳一次；不以關鍵字截斷科目。"""
        namespaces = list(dict.fromkeys(item.namespace for item in self.items))
        namespace_ids = {ns: index for index, ns in enumerate(namespaces)}
        return {
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_scope": self.source_scope,
            "catalog_kind": "observed_items_union",
            "availability": "未查核個別公司、期間及維度的資料列",
            "namespaces": namespaces,
            "item_columns": ["item_id", "label", "element", "namespace_index", "observed_reports"],
            "statements": {
                statement: [
                    [item.item_id, item.label, item.element,
                     namespace_ids[item.namespace], item.observed_reports]
                    for item in self.items if item.statement == statement
                ] for statement in STATEMENTS
            },
        }

    def to_judgment_state(self) -> dict:
        """提供所有科目的語意；完整 QName 與報告份數另存於 to_state。"""
        elements_by_label = {}
        for item in self.items:
            elements_by_label.setdefault(item.label, set()).add(item.element)
        statements = {}
        for statement in STATEMENTS:
            grouped = {}
            for item in self.items:
                if item.statement == statement:
                    grouped.setdefault((item.label, item.element), []).append(item.item_id)
            lines = []
            for (label, element), item_ids in grouped.items():
                line = ",".join(item_ids) + "|" + label
                if len(elements_by_label[label]) > 1:
                    line += "|" + element
                lines.append(line)
            statements[statement] = "\n".join(lines)
        return {
            "source_name": self.source_name, "source_sha256": self.source_sha256,
            "source_scope": self.source_scope, "catalog_kind": "observed_items_union",
            "representation": "all_items_labels_with_disambiguation_v1",
            "item_count": len(self.items),
            "availability": "未查核個別公司、期間及維度的資料列",
            "item_columns": ["comma_separated_item_ids", "label", "element_if_ambiguous"],
            "statements": statements,
        }

    def to_jev_state(self) -> dict:
        """二元判斷不需回傳 ID；保留每一種科目涵義及報表分類。"""
        view = self.to_judgment_state()
        return {
            **view,
            "representation": "all_items_labels_without_ids_v1",
            "item_columns": ["label", "element_if_ambiguous"],
            "statements": {
                statement: "\n".join(line.split("|", 1)[1] for line in text.splitlines())
                for statement, text in view["statements"].items()
            },
        }


def load_catalog(path: Path) -> DatabaseCatalog:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    scope_match = re.search(r"## 範圍與口徑\s*\n(.*?)\n## 項目清單", text, re.S)
    if scope_match is None:
        raise ValueError("科目清單缺少範圍與口徑")
    items = []
    expected = {}
    seen = set()
    statement = None
    for line_number, line in enumerate(text.splitlines(), 1):
        heading = HEADER.fullmatch(line.strip())
        if heading:
            statement, count = heading.groups()
            if statement in expected:
                raise ValueError(f"科目清單重複報表：{statement}")
            expected[statement] = int(count)
            continue
        if line.startswith("### "):
            raise ValueError(f"無法識別第 {line_number} 行的報表名稱")
        if not line.startswith("|") or statement is None:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "會計項目" or cells[0].startswith("---"):
            continue
        if len(cells) != 4 or not cells[3].isdigit():
            raise ValueError(f"科目清單第 {line_number} 行格式不正確")
        label, element, namespace, count = cells
        element, namespace = element.strip("`"), namespace.strip("`")
        identity = (statement, namespace, element)
        if identity in seen:
            raise ValueError(f"同一報表的 QName 重複：第 {line_number} 行")
        seen.add(identity)
        items.append(AccountingItem(
            item_id=f"i{len(items) + 1:04d}", statement=statement,
            label=label, element=element, namespace=namespace,
            observed_reports=int(count),
        ))
    counts = Counter(item.statement for item in items)
    if set(expected) != set(STATEMENTS) or dict(counts) != expected:
        raise ValueError("科目清單不完整，或各報表科目數與標題不符")
    return DatabaseCatalog(
        source_name=path.name, source_sha256=hashlib.sha256(raw).hexdigest(),
        source_scope=scope_match.group(1).strip(), items=tuple(items),
    )
