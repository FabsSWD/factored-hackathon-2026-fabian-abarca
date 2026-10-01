"""
Render the documentation diagrams as SVG on a fixed grid.

The diagrams are generated from the data structures below so that every node
sits on a column and row, every line is orthogonal, and no line crosses a node.
Edit this script (not the SVG files) and re-run it:

    python scripts/render_diagrams.py

Outputs:
    docs/diagrams/images/dispute-decision-flow.svg
    docs/diagrams/images/dispute-case-lifecycle.svg
"""
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "diagrams" / "images"

FONT = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
INK = "#1E293B"
MUTED = "#475569"
LINE = "#64748B"

STYLE = {
    "terminal": {"fill": "#1E293B", "stroke": "#1E293B", "text": "#FFFFFF"},
    "decision": {"fill": "#F1F5F9", "stroke": "#475569", "text": INK},
    "process": {"fill": "#FFFFFF", "stroke": "#475569", "text": INK},
    "human": {"fill": "#F1F5F9", "stroke": "#94A3B8", "text": INK},
    "RESOLVE": {"fill": "#DCFCE7", "stroke": "#16A34A", "text": INK},
    "ESCALATE": {"fill": "#FFEDD5", "stroke": "#EA580C", "text": INK},
    "INFORM": {"fill": "#DBEAFE", "stroke": "#2563EB", "text": INK},
    "CLARIFY": {"fill": "#FEF9C3", "stroke": "#CA8A04", "text": INK},
    "REFUSE": {"fill": "#FEE2E2", "stroke": "#DC2626", "text": INK},
}


class Svg:
    def __init__(self, width: int, height: int, title: str):
        self.w, self.h = width, height
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" font-family="{FONT}" role="img" aria-label="{escape(title)}">',
            f"<title>{escape(title)}</title>",
            "<defs><marker id='arrow' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' "
            f"markerHeight='7' orient='auto-start-reverse'><path d='M0,0 L10,5 L0,10 z' fill='{LINE}'/></marker></defs>",
            f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>',
        ]

    def text(self, cx, cy, lines, size=12, color=INK, weight="normal", anchor="middle"):
        lh = size + 4
        y0 = cy - (len(lines) - 1) * lh / 2
        for i, line in enumerate(lines):
            w = "bold" if (weight == "first-bold" and i == 0) or weight == "bold" else "normal"
            self.parts.append(
                f'<text x="{cx}" y="{y0 + i * lh}" font-size="{size}" fill="{color}" '
                f'font-weight="{w}" text-anchor="{anchor}" dominant-baseline="central">{escape(line)}</text>'
            )

    def box(self, cx, cy, w, h, kind, lines, rx=6):
        s = STYLE[kind]
        self.parts.append(
            f'<rect x="{cx - w / 2}" y="{cy - h / 2}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{s["fill"]}" stroke="{s["stroke"]}" stroke-width="1.5"/>'
        )
        weight = "first-bold" if kind in ("RESOLVE", "ESCALATE", "INFORM", "CLARIFY", "REFUSE") else "normal"
        self.text(cx, cy, lines, color=s["text"], weight=weight)

    def pill(self, cx, cy, w, h, lines):
        self.box(cx, cy, w, h, "terminal", lines, rx=h / 2)

    def diamond(self, cx, cy, w, h, lines):
        s = STYLE["decision"]
        pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
        self.parts.append(f'<polygon points="{pts}" fill="{s["fill"]}" stroke="{s["stroke"]}" stroke-width="1.5"/>')
        self.text(cx, cy, lines, size=11.5)

    def line(self, points, arrow=True):
        pts = " ".join(f"{x},{y}" for x, y in points)
        marker = ' marker-end="url(#arrow)"' if arrow else ""
        self.parts.append(f'<polyline points="{pts}" fill="none" stroke="{LINE}" stroke-width="1.4"{marker}/>')

    def dot(self, x, y):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="3.2" fill="{LINE}"/>')

    def label(self, x, y, txt, anchor="start"):
        self.text(x, y, [txt], size=11, color=MUTED, anchor=anchor)

    def legend(self, x, y, items):
        cx = x
        for kind, name in items:
            s = STYLE[kind]
            self.parts.append(
                f'<rect x="{cx}" y="{y - 8}" width="16" height="16" rx="3" fill="{s["fill"]}" stroke="{s["stroke"]}" stroke-width="1.5"/>'
            )
            self.text(cx + 22, y, [name], size=11.5, color=INK, anchor="start")
            cx += 50 + len(name) * 7.4

    def save(self, name):
        self.parts.append("</svg>")
        (OUT / name).write_text("\n".join(self.parts), encoding="utf-8")
        print(f"wrote {OUT / name}")


# --------------------------------------------------------------------------
# Decision flow: one evaluation per customer message, one start, one end.
# --------------------------------------------------------------------------
# (kind, lines, side_outcome, side_label). side_outcome = (outcome_kind, lines).
FLOW = [
    ("terminal", ["Start: customer message received"], None, None),
    ("decision", ["Interrupt trigger?", "ESC-03, ESC-05, ESC-06, ESC-13"],
     ("ESCALATE", ["ESCALATE", "Card block (ACT-03) offered first on ESC-03"]), "yes"),
    ("decision", ["GATE-01", "Language es or pt?"],
     ("CLARIFY", ["CLARIFY preferred language once", "then ESCALATE (ESC-12)"]), "no"),
    ("decision", ["GATE-02", "Authenticated session?"],
     ("CLARIFY", ["CLARIFY: request authentication", "INFORM if the customer declines"]), "no"),
    ("decision", ["GATE-03", "Customer Active?"],
     ("ESCALATE", ["ESCALATE", "ESC-08: ineligible status"]), "no"),
    ("decision", ["GATE-04", "Records owned by customer?"],
     ("REFUSE", ["REFUSE", "Log security event"]), "no"),
    ("decision", ["GATE-05", "One transaction identified?"],
     ("CLARIFY", ["CLARIFY transaction_ref", "ESCALATE (ESC-09) at the limit"]), "no"),
    ("decision", ["GATE-06", "Status Approved?"],
     ("INFORM", ["INFORM", "Pending, Declined, or Reversed"]), "no"),
    ("decision", ["GATE-07", "Type and reason marked A?"],
     ("ESCALATE", ["ESCALATE (ESC-14) if marked H", "INFORM if marked N"]), "no"),
    ("decision", ["GATE-08", "Within DISPUTE_WINDOW_DAYS?"],
     ("ESCALATE", ["ESCALATE (ESC-07) if late", "INFORM if past LATE_WINDOW_DAYS"]), "no"),
    ("decision", ["GATE-09", "Product Active or Blocked?"],
     ("ESCALATE", ["ESCALATE", "ESC-08: ineligible status"]), "no"),
    ("decision", ["GATE-11", "No case for it yet?"],
     ("INFORM", ["INFORM", "Existing case_ref and status"]), "no"),
    ("decision", ["Record triggers clear?", "ESC-01, ESC-02, ESC-04"],
     ("ESCALATE", ["ESCALATE, no slot questions", "Missing slots in open_questions"]), "no"),
    ("decision", ["GATE-10", "Reason preconditions met?"],
     ("CLARIFY", ["CLARIFY next slot, or", "INFORM / ESCALATE per policy §5"]), "no"),
    ("decision", ["Decision layer confident?", "ESC-11"],
     ("ESCALATE", ["ESCALATE", "ESC-11: model uncertainty"]), "no"),
    ("decision", ["Explicit confirmation?", "COM-03"],
     ("CLARIFY", ["CLARIFY: present summary", "INFORM if the customer declines"]), "no"),
    ("process", ["ACT-02 create case", "ACT-04 record credit flag"], None, None),
    ("decision", ["Read-back verified?", "within TOOL_MAX_RETRIES"],
     ("ESCALATE", ["ESCALATE", "ESC-10: tool failure"]), "no"),
    ("RESOLVE", ["RESOLVE", "Show verified case_ref"], None, None),
    ("process", ["Send templated reply (COM-02)", "Write audit record"], None, None),
    ("terminal", ["End of turn"], None, None),
]


def render_flow():
    spine_x, row_h, top = 200, 96, 50
    dw, dh = 290, 78          # decision size
    bw, bh = 250, 50          # spine box size
    ow, oh = 280, 52          # outcome box size
    out_x = spine_x + dw / 2 + 60 + ow / 2
    bus_x = out_x + ow / 2 + 40
    legend_y = top + row_h * len(FLOW) + 10
    svg = Svg(int(bus_x + 40), int(legend_y + 40), "Dispute decision flow")

    ys = [top + i * row_h for i in range(len(FLOW))]
    fin_index = len(FLOW) - 2
    joins = []

    for i, (kind, lines, side, side_label) in enumerate(FLOW):
        y = ys[i]
        if kind == "terminal":
            svg.pill(spine_x, y, bw, 40, lines)
            half = 20
        elif kind == "decision":
            svg.diamond(spine_x, y, dw, dh, lines)
            half = dh / 2
        else:
            svg.box(spine_x, y, bw, bh, kind, lines)
            half = bh / 2

        if i < len(FLOW) - 1:
            nxt_kind = FLOW[i + 1][0]
            nxt_half = dh / 2 if nxt_kind == "decision" else (20 if nxt_kind == "terminal" else bh / 2)
            svg.line([(spine_x, y + half), (spine_x, ys[i + 1] - nxt_half)])
            if kind == "decision":
                svg.label(spine_x + 8, y + half + 9, "yes" if side_label == "no" else "no")

        if side:
            okind, olines = side
            svg.line([(spine_x + dw / 2, y), (out_x - ow / 2, y)])
            svg.label(spine_x + dw / 2 + 10, y - 9, side_label)
            svg.box(out_x, y, ow, oh, okind, olines)
            svg.line([(out_x + ow / 2, y), (bus_x, y)], arrow=False)
            joins.append(y)

    fin_y = ys[fin_index]
    svg.line([(bus_x, joins[0]), (bus_x, fin_y), (spine_x + bw / 2, fin_y)])
    for y in joins[1:]:
        svg.dot(bus_x, y)

    svg.legend(spine_x - dw / 2, legend_y, [
        ("RESOLVE", "RESOLVE"), ("CLARIFY", "CLARIFY"), ("INFORM", "INFORM"),
        ("ESCALATE", "ESCALATE"), ("REFUSE", "REFUSE"),
    ])
    svg.save("dispute-decision-flow.svg")


# --------------------------------------------------------------------------
# Case lifecycle: one start, one end, system-owned vs human-owned steps.
# --------------------------------------------------------------------------
def render_lifecycle():
    L, M, R, X = 150, 400, 650, 880
    row = {i: 50 + i * 100 for i in range(10)}
    bw, bh, dw, dh = 230, 50, 250, 72
    svg = Svg(1010, row[9] + 30, "Dispute case lifecycle")

    def elbow_down(x_from, y_from, x_to, y_to, mid):
        svg.line([(x_from, y_from), (x_from, mid), (x_to, mid), (x_to, y_to)])

    # Spine: start, draft, conversation outcome
    svg.pill(M, row[0], bw, 40, ["Start: dispute raised"])
    svg.box(M, row[1], bw, bh, "process", ["Draft", "Exists only in the conversation"])
    svg.diamond(M, row[2], dw, dh, ["Conversation outcome?"])
    svg.line([(M, row[0] + 20), (M, row[1] - bh / 2)])
    svg.line([(M, row[1] + bh / 2), (M, row[2] - dh / 2)])

    # Three exits: left, bottom, right. No shared segments.
    top3 = row[3] - bh / 2
    svg.box(L, row[3], bw, bh, "RESOLVE", ["Open", "System: ACT-02 verified"])
    svg.box(M, row[3], bw, bh, "ESCALATE", ["Escalated", "System: ACT-05 handoff"])
    svg.box(X, row[3], bw - 40, bh, "INFORM", ["No case", "INFORM or REFUSE"])
    svg.line([(M - dw / 2, row[2]), (L, row[2]), (L, top3)])
    svg.line([(M, row[2] + dh / 2), (M, top3)])
    svg.line([(M + dw / 2, row[2]), (X, row[2]), (X, top3)])
    label_y = (row[2] + top3) / 2 + 4
    svg.label(L + 8, label_y, "RESOLVE")
    svg.label(M + 8, label_y + 10, "ESCALATE")
    svg.label(X + 8, label_y, "INFORM / REFUSE")

    # Human back office
    svg.box(M, row[4], bw, bh, "human", ["In Process", "Human: back-office review"])
    elbow_down(L, row[3] + bh / 2, M - 60, row[4] - bh / 2, row[3] + 50)
    svg.line([(M, row[3] + bh / 2), (M, row[4] - bh / 2)])
    svg.diamond(M, row[5], dw, dh, ["Back-office decision?"])
    svg.line([(M, row[4] + bh / 2), (M, row[5] - dh / 2)])

    top6 = row[6] - bh / 2
    svg.box(L, row[6], bw, bh, "human", ["Resolved", "Human: in the customer's favor"])
    svg.box(R, row[6], bw, bh, "human", ["Rejected", "Human: claim not upheld"])
    svg.line([(M - dw / 2, row[5]), (L, row[5]), (L, top6)])
    svg.line([(M + dw / 2, row[5]), (R, row[5]), (R, top6)])
    label_y = (row[5] + top6) / 2 + 4
    svg.label(L + 8, label_y, "upheld")
    svg.label(R + 8, label_y, "not upheld")

    svg.box(M, row[7], bw, bh, "human", ["Closed", "Human: customer notified"])
    elbow_down(L, row[6] + bh / 2, M - 60, row[7] - bh / 2, row[6] + 50)
    elbow_down(R, row[6] + bh / 2, M + 60, row[7] - bh / 2, row[6] + 50)

    svg.pill(M, row[8], bw, 40, ["End"])
    svg.line([(M, row[7] + bh / 2), (M, row[8] - 20)])
    svg.line([(X, row[3] + bh / 2), (X, row[8]), (M + bw / 2, row[8])])

    svg.legend(L - bw / 2, row[9] - 20, [
        ("RESOLVE", "System creates case"), ("ESCALATE", "System hands off"),
        ("INFORM", "No case"), ("human", "Human only"),
    ])
    svg.save("dispute-case-lifecycle.svg")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    render_flow()
    render_lifecycle()
