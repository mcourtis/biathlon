"Export results as Markdown tables suitable for Reddit."

from typing import Sequence

def to_markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """
    Convert headers and rows into a simple Reddit-compatible Markdown table.
    """
    # Header row
    md = []
    md.append("| " + " | ".join(headers) + " |")
    md.append("|" + "|".join(["---"] * len(headers)) + "|")

    # Data rows
    for row in rows:
        md.append("| " + " | ".join(row) + " |")

    return "\n".join(md)
