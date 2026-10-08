# textbook/(Graduate_Studies_in_Mathematics,034)_Sigurdur_Helgason_-_Differential_Geometry,_Lie_Groups,_and_Symmetric_Spaces_(2001,_American_Mathematical_Society).pdf_143 — page 0

Proposition 7.2. Let $X_{1},\,...,\,X_{n}$ be a basis of $\mathfrak{g}$ and $\omega_{1},\,...,\,\omega_{n}$ the 1-forms on $G$ determined by $\omega_{i}(\tilde{X}_{j})=\delta_{i j},$ Then

$$
d\omega_{i}=-\textstyle{\frac{1}{2}}\sum_{j,k=1}^{n}c^{i}{}_{j k}\omega_{j}\ \wedge\ \omega_{k}
$$

$c_{\ j k}^{i}$ are the structural constants given by

$$
[X_{j},X_{k}]=\sum_{i=1}^{n}c^{i}{}_{j k}X_{i}.
$$

Equations (3) are known as the Maurer-Cartan equations. They follow immediately from (1). They also follow from Theorem 8.1, Chapter I if we give $G$ the left invariant affine connection for which $\alpha$ in Prop. 1.4 is identically 0. Note that the Jacobi identity for $\mathfrak{g}$ is reflected in the relation $d^{2}=0$

Example. Consider as in $\S1$ the general linear group $G L(n,\,R)$ with the usual coordinates $\sigma\to(x_{i j}(\sigma))$ . Writing $X=(x_{i j})$ $d X=(d x_{i j})$ ，the matrix

$$
\varOmega=X^{-1}\,d X,
$$

whose entries are 1-forms on $G$ is invariant under left translations $X\to\sigma X$ on $G$ . Writing

$$
d X=X\Omega,
$$

we can derive

$$
0=(d X)\wedge\Omega+X\wedge\,d\Omega,
$$

where $\wedge$ denote the obvious wedge product of matrices. Multiplying by $X^{-1}$ , we obtain

$$
d\Omega+\Omega\wedge\Omega=0,
$$

which is an equivalent form of (3)

More generally, consider for each $\pmb{x}$ in the Lie group $G$ the mapping

$$
d L(x^{-1})_{x}:G_{x}\to\mathfrak{g}
$$

and let $\pmb{\Omega}$ denote the family of these maps. In other words,

$$
\varOmega_{x}(v)=d L(x^{-1})(v)\qquad\quad\mathrm{if}\quad v\in G_{x}.
$$

Then $\Omega$ is a 1-form on $G$ with values in $\mathfrak{g}$ . Moreover, if $x,\,y\in G,$ then

$$
\mathcal{Q}_{x y}\circ d L(x)_{y}=\mathcal{Q}_{y},
$$
