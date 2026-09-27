"""Render the companion Markdown as one self-contained, Overleaf-ready file.

This deliberately supports only the Markdown constructs used in report.md.
The output embeds its TikZ figure and bibliography; Overleaf needs main.tex only.
"""
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent

PREAMBLE = r"""% Self-contained: paste this entire file into Overleaf as main.tex.
% Compiler: pdfLaTeX (Overleaf default). No external images or .bib files.
% Generated from report.md; this file can also be edited independently.
\documentclass[11pt,a4paper]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage[margin=25mm]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{booktabs,longtable,array}
\usepackage{xcolor}
\usepackage{tikz}
\usetikzlibrary{arrows.meta,positioning}
\usepackage{listings}
\usepackage{hyperref}
\definecolor{ink}{HTML}{203345}
\definecolor{accent}{HTML}{246A78}
\hypersetup{colorlinks=true,linkcolor=accent,urlcolor=accent,citecolor=accent,
  pdftitle={Ansatz: An Inspectable Workspace for Multi-Engine Mathematical Research},
  pdfsubject={Repository technical report, 15 September 2026}}
\urlstyle{tt}
\lstset{basicstyle=\ttfamily\footnotesize,breaklines=true,
  columns=fullflexible,keepspaces=true,showstringspaces=false,
  frame=single,rulecolor=\color{black!15},xleftmargin=5pt,xrightmargin=5pt}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.5em}
\setlength{\emergencystretch}{3em}
\renewcommand{\arraystretch}{1.18}
\setcounter{tocdepth}{2}
\title{\textbf{Ansatz: An Inspectable Workspace for\\Multi-Engine Mathematical Research}}
\author{Repository technical report}
\date{15 September 2026}
\begin{document}
\maketitle
"""

FIGURE = r"""
\begin{figure}[htbp]
\centering
\begin{tikzpicture}[
  box/.style={draw=accent!65,fill=accent!4,rounded corners=2pt,
    align=center,text width=3.7cm,minimum height=1.05cm,font=\small},
  flow/.style={-{Stealth[length=2mm]},thick,draw=ink},
  optional/.style={flow,dashed}]
\node[box] (user) at (0,0) {Researcher / project\\Problem and task};
\node[box] (console) at (5.1,0) {Console and job manager\\Context, owner, workspace};
\node[box] (runners) at (10.2,0) {Engine adapters\\Model and tool execution};
\node[box] (checks) at (0,-2.1) {Optional analysis\\Lean checks / proof tools};
\node[box] (artifacts) at (5.1,-2.1) {Persistent run artifacts\\Proofs, traces, check records};
\node[box] (views) at (10.2,-2.1) {Inspection and sharing\\Pipeline, graphs, snapshots};
\draw[flow] (user) -- (console);
\draw[flow] (console) -- (runners);
\draw[flow] (console) -- (artifacts);
\draw[flow] (runners.south) -- ++(0,-0.45) -| (artifacts.north east);
\draw[optional] (artifacts) -- (checks);
\draw[optional] (checks.north) -- ++(0,0.42) -| (artifacts.north west);
\draw[flow] (artifacts) -- (views);
\end{tikzpicture}
\caption{Main components and evidence flow. Dashed connections indicate
optional analysis. Execution traces and proof dependencies are distinct artifacts.}
\end{figure}
"""

BIBLIOGRAPHY = r"""
\begin{thebibliography}{9}
\bibitem{prove2me}
Shuze Chen, Kunal Marwaha, Xiaoyang Lu, Henry Yuen, and Tianyi Peng.
\newblock \emph{Prove2Me: An Open Collaborative Platform for Scaling Math Formalization}.
\newblock arXiv:2608.28433v1, 2026.
\newblock \url{https://arxiv.org/abs/2608.28433v1}.
\newblock Structural example and design comparison; its case-study outcomes
are not attributed to Ansatz.

\bibitem{lean4}
Leonardo de Moura and Sebastian Ullrich.
\newblock \emph{The Lean 4 Theorem Prover and Programming Language}.
\newblock Automated Deduction---CADE 28, pp. 625--635, 2021.
\newblock DOI: \href{https://doi.org/10.1007/978-3-030-79876-5_37}
{10.1007/978-3-030-79876-5\_37}.
\newblock \url{https://lean-lang.org/papers/lean4.pdf}.

\bibitem{repository}
Agent Monitor / Ansatz repository contributors.
\newblock \emph{Inspected working-tree source and public artifacts}.
\newblock 15 September 2026. Git base
\nolinkurl{a2f1f22a3a394a518abebe7a73a312fa98b1f31a}.
\newblock The companion evidence manifest identifies the uncommitted source
snapshot. Appendix A maps claims to repository files.
Third-party components retain their attribution notices.
\end{thebibliography}
"""


def escape(text):
    substitutions = {
        "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
        "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        "→": r"$\rightarrow$", "≥": r"$\geq$", "≤": r"$\leq$",
        "–": "--", "—": "---", "−": "-", "·": r"\textperiodcentered{}",
        "‘": "`", "’": "'", "“": "``", "”": "''", "…": r"\ldots{}",
        "ő": r"\H{o}", "Ő": r"\H{O}",
    }
    return "".join(substitutions.get(char, char) for char in text)


TOKEN = re.compile(r"(`[^`]+`|\*\*.+?\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\)|\$[^$]+\$|\[R[123]\])")


def inline(text):
    chunks = []
    position = 0
    for match in TOKEN.finditer(text):
        chunks.append(escape(text[position:match.start()]))
        token = match.group()
        if token.startswith("`"):
            raw = token[1:-1]
            chunks.append(r"\texttt{" + escape(raw) + "}" if " " in raw else r"\nolinkurl{" + raw + "}")
        elif token.startswith("**"):
            chunks.append(r"\textbf{" + inline(token[2:-2]) + "}")
        elif token.startswith("*"):
            chunks.append(r"\emph{" + inline(token[1:-1]) + "}")
        elif token.startswith("$"):
            chunks.append(token)
        elif re.fullmatch(r"\[R[123]\]", token):
            key = {"[R1]": "prove2me", "[R2]": "lean4", "[R3]": "repository"}[token]
            chunks.append(r"\cite{" + key + "}")
        else:
            link = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", token)
            label, target = link.groups()
            if target.startswith("../../"):
                chunks.append(r"\nolinkurl{" + target[6:] + "}")
            else:
                chunks.append(r"\href{" + target + "}{" + inline(label) + "}")
        position = match.end()
    chunks.append(escape(text[position:]))
    return "".join(chunks)


def table(lines):
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    header, body = rows[0], rows[2:]
    n = len(header)
    weights = {2: [0.31, 0.69], 3: [0.23, 0.38, 0.39],
               5: [0.44, 0.14, 0.14, 0.14, 0.14],
               7: [0.10, 0.12, 0.17, 0.17, 0.15, 0.15, 0.14]}[n]
    spec = "".join(r">{\raggedright\arraybackslash}p{" + str(w) + r"\dimexpr\linewidth-" + str(2*n) + r"\tabcolsep\relax}" for w in weights)
    heading = " & ".join(r"\textbf{" + inline(cell) + "}" for cell in header) + r" \\"
    result = [r"\begingroup\small", r"\setlength{\tabcolsep}{4pt}", r"\begin{longtable}{"+spec+"}", r"\toprule", heading, r"\midrule\endfirsthead", r"\toprule", heading, r"\midrule\endhead", r"\bottomrule\endfoot"]
    for row in body:
        if len(row) != n:
            raise ValueError(f"Malformed table row: {row}")
        result.append(" & ".join(inline(cell) for cell in row) + r" \\")
    result += [r"\end{longtable}", r"\endgroup"]
    return "\n".join(result)


def build():
    lines = (HERE / "report.md").read_text().splitlines()
    result = [PREAMBLE]
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith("# ") or line.startswith("**Repository technical report"):
            i += 1
            continue
        if line == "## References":
            result.append(BIBLIOGRAPHY)
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                i += 1
            continue
        if line.startswith("## Abstract"):
            i += 1
            abstract = []
            while i < len(lines) and not lines[i].startswith("## "):
                abstract.append(lines[i]); i += 1
            result.append(r"\begin{abstract}" + "\n" + inline(" ".join(abstract).strip()) + "\n" + r"\end{abstract}")
            continue
        if line.startswith("## ") or line.startswith("### "):
            level = "subsection" if line.startswith("### ") else "section"
            title = line.lstrip("# ")
            if title.startswith("Appendix "):
                result.append(r"\clearpage")
            result.append("\\"+level+"*{"+inline(title)+"}")
            i += 1
            continue
        if line.startswith("!["):
            result.append(FIGURE)
            i += 1
            continue
        if line.startswith("```"):
            i += 1
            block = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i]); i += 1
            result.append(r"\begin{lstlisting}"+"\n"+"\n".join(block)+"\n"+r"\end{lstlisting}")
            i += 1
            continue
        if line == "$$":
            i += 1
            block = []
            while i < len(lines) and lines[i] != "$$":
                block.append(lines[i]); i += 1
            result.append("\\[\n" + "\n".join(block) + "\n\\]")
            i += 1
            continue
        if line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i]); i += 1
            result.append(table(block))
            continue
        if re.match(r"\d+\. ", line):
            result.append(r"\begin{enumerate}")
            while i < len(lines) and re.match(r"\d+\. ", lines[i]):
                result.append(r"\item " + inline(re.sub(r"^\d+\. ", "", lines[i])))
                i += 1
            result.append(r"\end{enumerate}")
            continue
        paragraph = [line]
        i += 1
        while i < len(lines) and lines[i].strip():
            paragraph.append(lines[i]); i += 1
        result.append(inline(" ".join(paragraph)))
    result.append(r"\end{document}")
    (HERE / "main.tex").write_text("\n\n".join(result) + "\n")
    print("Wrote self-contained main.tex")


if __name__ == "__main__":
    build()
