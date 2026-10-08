# textbook/GTM40-Denumerable_Markov_Chains-with_a_chapter_of_Markov_Random_Fields_by_David_Griffeath1976.pdf_256 — page 0

PROOF: Let $\epsilon > 0$ be given. Choose $N$ sufficiently large that $\textstyle\sum_{k\,>\,N}a_{k}\,<\,\epsilon/(4B)$ and pick $N^{\prime}$ large enough so that for all $n \geq N^{\prime}$  

$$
\left|b_{n}\,-\,b_{n\,-1}\right|\,\leq\,\frac{\epsilon}{2a N}.
$$  

Then for $\pmb{n} \geq N + N^{\prime}$, we have  

$$
\begin{array}{r l}{\bigg|\displaystyle\sum_{k=0}^{n}a_{k}(b_{n}\,-\,b_{n-k})\bigg|\,}&{\le\,\displaystyle\sum_{k=0}^{N}a_{k}\big|b_{n}\,-\,b_{n-k}\big|\,+\,\displaystyle\sum_{k=N+1}^{n}a_{k}\big(\big|b_{n}\big|\,+\,\big|b_{n-k}\big|\big)\,}\\ &{\le\,\displaystyle\sum_{k=0}^{N}a_{k}\,\displaystyle\sum_{j=0}^{k-1}\big|b_{n-j}\,-\,b_{n-j-1}\big|\,+\,2B\,\displaystyle\sum_{k=N+1}^{n}a_{k}\,}\\ &{\le\,\displaystyle\sum_{k=0}^{N}a_{k}\,\displaystyle\sum_{j=0}^{k-1}\frac{\epsilon}{2a N}\,+\,\frac{\epsilon}{4B}\cdot2B,\quad\mathrm{since}\,\,\,n\,-\,j\,\ge\,N^{\prime}\,}\\ &{\le\,\displaystyle\frac{\epsilon}{2a}\,\displaystyle\sum_{k=0}^{N}a_{k}\,+\,\frac{\epsilon}{2}\,}\\ &{\le\,\epsilon.}&\end{array}
$$  

Theorem 9-7: Let $i, j$ and $k$ be arbitrary states in a current Markov chain which is either null or noncyclic ergodic. Then  

$$
\operatorname*{lim}_{n\,\rightarrow\,\infty}\,[(N_{k k}^{(n)}\,-\,N_{i k}^{(n)})\alpha_{j}/\alpha_{k}\,+\,N_{i j}^{(n)}\,-\,N_{k j}^{(n)}]\,=\,{^k N}_{i j}.
$$  

PROOF: We may assume that neither $i$ nor $j$ equals $k$ since otherwise both sides are clearly zero. We begin by establishing four equations:  

$$
\begin{array}{l l}{{(1)}}&{{N_{k k}^{(n)}=\displaystyle\sum_{\nu=0}^{\infty}F_{i k}^{(\nu)}N_{k k}^{(n)}}}\\ {{(2)}}&{{N_{i k}^{(n)}=\displaystyle\sum_{\nu=0}^{n}F_{i k}^{(\nu)}N_{k k}^{(n-\nu)}}}\\ {{(3)}}&{{N_{k j}^{(n)}=\displaystyle\sum_{\nu=0}^{\infty}F_{i k}^{(\nu)}N_{k j}^{(n)}}}\\ {{(4)}}&{{N_{i j}^{(n)}=\displaystyle\sum_{\nu=0}^{n}F_{i k}^{(\nu)}N_{k j}^{(n-\nu)}\,+\,{^k}N_{i j}^{(n)}.}}\end{array}
$$  

Equations (1) and (3) follow the fact that $\sum\,F_{i k}^{(v)}\,=\,H_{i k}\,=\,1\,$ Equation (2) comes from Theorem 4-11 with the random time $\mathbf t = \operatorname*{min} (\mathbf{t}_{k},\,n)$, and equation (4) is a similar result, except that the sum has been broken into two parts representing what happens after and before state $k$ is reached for the first time.
