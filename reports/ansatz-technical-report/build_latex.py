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
% Layout adapted from the mentor's shared arxiv_version template:
% https://www.overleaf.com/read/wszynkpnmpks#fe3644
% All required styling is embedded here; no custom .sty or logo files needed.
\documentclass[10pt,letterpaper]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{tgpagella}
\usepackage{mathpazo}
\usepackage{inconsolata}
\usepackage[letterpaper,margin=2.5cm,headheight=24pt]{geometry}
\usepackage{microtype}
\usepackage{amsmath,amssymb,amsthm}
\usepackage{booktabs,longtable,array}
\usepackage{xcolor}
\usepackage{graphicx}
\usepackage{tikz}
\usetikzlibrary{arrows.meta,positioning}
\usepackage[most]{tcolorbox}
\usepackage{fancyhdr}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage{needspace}
\usepackage{listings}
\usepackage[numbers,square]{natbib}
\usepackage{xurl}
\usepackage{hyperref}
\definecolor{ink}{HTML}{203345}
\definecolor{accent}{RGB}{0,82,155}
\definecolor{darkblue}{rgb}{0,0,0.5}
\definecolor{headergray}{HTML}{4D4D4D}
\hypersetup{colorlinks=true,linkcolor=darkblue,urlcolor=darkblue,citecolor=darkblue,
  pdftitle={Math Framework LLM Assistant For Research},
  pdfauthor={Richard C, Eric J},
  pdfsubject={Ansatz technical report; revised 20 September 2026}}
\urlstyle{tt}
\lstset{basicstyle=\ttfamily\footnotesize,breaklines=true,
  columns=fullflexible,keepspaces=true,showstringspaces=false,
  frame=lines,rulecolor=\color{accent!40},xleftmargin=5pt,xrightmargin=5pt,
  literate={ℝ}{{$\mathbb{R}$}}1 {ℕ}{{$\mathbb{N}$}}1
    {ℚ}{{$\mathbb{Q}$}}1 {∀}{{$\forall$}}1 {∃}{{$\exists$}}1
    {¬}{{$\neg$}}1 {∈}{{$\in$}}1 {→}{{$\to$}}1
    {↔}{{$\leftrightarrow$}}1 {≠}{{$\ne$}}1 {≤}{{$\le$}}1
    {≥}{{$\ge$}}1 {⟨}{{$\langle$}}1 {⟩}{{$\rangle$}}1}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.5pc}
\setlength{\emergencystretch}{3em}
\widowpenalty=10000
\clubpenalty=10000
\renewcommand{\arraystretch}{1.18}
\renewcommand{\topfraction}{0.95}
\renewcommand{\textfraction}{0.05}
\setlist{topsep=4pt,itemsep=2pt,parsep=2pt,leftmargin=3pc}
\setcounter{tocdepth}{2}
\titleformat{\section}{\large\bfseries\raggedright}{\thesection}{0.7em}{}
\titleformat{\subsection}{\normalsize\bfseries\raggedright}{\thesubsection}{0.7em}{}
\titleformat{\subsubsection}{\normalsize\bfseries\raggedright}{\thesubsubsection}{0.7em}{}
\titlespacing*{\section}{0pt}{2ex plus .5ex minus .2ex}{1.5ex plus .3ex minus .2ex}
\titlespacing*{\subsection}{0pt}{1.8ex plus .5ex minus .2ex}{.8ex plus .2ex}
\titlespacing*{\subsubsection}{0pt}{1.5ex plus .5ex minus .2ex}{.5ex plus .2ex}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small\color{headergray}Ansatz\quad Technical report}
\fancyhead[R]{\small\color{headergray}20 September 2026}
\fancyfoot[C]{\thepage}
\renewcommand{\headrulewidth}{1pt}
\renewcommand{\footrulewidth}{0pt}
\fancypagestyle{firstpage}{\fancyfoot[C]{}}
\makeatletter
\renewcommand{\headrule}{{\color{headergray}\hrule
  \@height\headrulewidth\@width\headwidth\vskip-\headrulewidth}}
\renewcommand{\maketitle}{%
  \vspace*{0.1in}
  \begin{center}
    {\fontsize{14}{16}\selectfont\bfseries\@title\par}
    \vspace{1em}
    {\normalsize\@author\par}
  \end{center}
  \vspace{0.15in}
  \thispagestyle{firstpage}}
\makeatother
\renewenvironment{abstract}
  {\begin{center}\begin{tcolorbox}[
     colframe=accent,colback=white,arc=2mm,boxrule=0.9pt,
     left=1mm,right=1mm,top=3mm,bottom=4mm,enhanced,width=0.95\textwidth]
     {\centering\large\bfseries\color{accent}Abstract\par}\vspace{1ex}
     \begin{quote}}
  {\end{quote}\end{tcolorbox}\end{center}}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{proposition}[theorem]{Proposition}
\title{Math Framework LLM Assistant For Research}
\author{Richard C, Eric J}
\date{20 September 2026}
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
               4: [0.12, 0.29, 0.29, 0.30],
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
    in_appendix = False
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
                if not in_appendix:
                    result.append(r"\appendix")
                    in_appendix = True
                title = re.sub(r"^Appendix [A-Z]\. ", "", title)
            else:
                title = re.sub(r"^\d+(?:\.\d+)*\. ", "", title)
            result.append("\\"+level+"{"+inline(title)+"}")
            i += 1
            continue
        if line.startswith("!["):
            result.append(FIGURE)
            i += 1
            continue
        if line.startswith("```"):
            language = line[3:].strip()
            i += 1
            block = []
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i]); i += 1
            if language == "latex":
                result.append("\n".join(block))
            else:
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
        paragraph_text = " ".join(paragraph)
        if paragraph_text.endswith(":"):
            result.append(r"\Needspace{6\baselineskip}")
        result.append(inline(paragraph_text))
    result.append(r"\end{document}")
    (HERE / "main.tex").write_text("\n\n".join(result) + "\n")
    print("Wrote self-contained main.tex")


if __name__ == "__main__":
    build()
