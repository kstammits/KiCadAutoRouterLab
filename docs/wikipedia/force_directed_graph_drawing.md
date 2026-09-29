Force-directed graph drawing

Physical simulation to visualize graphs

[![](//thumb.wikimedia.org/wikipedia/commons/thumb/2/22/SocialNetworkAnalysis.png/250px-SocialNetworkAnalysis.png?utm_source=en.wikipedia.org&utm_campaign=parser&utm_content=thumbnail)](./File:SocialNetworkAnalysis.png)

Social network visualization using a force-directed graph drawing algorithm[[1]](./Force-directed_graph_drawing#cite_note-1)

[![](//thumb.wikimedia.org/wikipedia/commons/thumb/9/90/Visualization_of_wiki_structure_using_prefuse_visualization_package.png/250px-Visualization_of_wiki_structure_using_prefuse_visualization_package.png?utm_source=en.wikipedia.org&utm_campaign=parser&utm_content=thumbnail)](./File:Visualization_of_wiki_structure_using_prefuse_visualization_package.png)

Visualization of links between pages on a wiki using a force-directed layout

**Force-directed graph drawing** algorithms are a class of [algorithms](./Algorithm "Algorithm") for [drawing graphs](./Graph_drawing "Graph drawing") in an aesthetically-pleasing way. Their purpose is to position the nodes of a [graph](./Graph_(discrete_mathematics) "Graph (discrete mathematics)") in two-dimensional or three-dimensional space so that all the edges are of more or less equal length and there are as few crossing edges as possible, by assigning forces among the set of edges and the set of nodes, based on their relative positions, and then using these forces either to simulate the motion of the edges and nodes or to minimize their energy.[[2]](./Force-directed_graph_drawing#cite_note-2)

While graph drawing can be a difficult problem, force-directed algorithms, being physical simulations, usually require no special knowledge about graph theory such as [planarity](./Planar_graph "Planar graph").

## Forces

Force-directed graph drawing algorithms assign forces among the set of edges and the set of nodes of a [graph drawing](./Graph_drawing "Graph drawing"). Typically, [spring](./Spring_(device) "Spring (device)")-like attractive forces based on [Hooke's law](./Hooke's_law "Hooke's law") are used to attract pairs of endpoints of the graph's edges towards each other, while simultaneously repulsive forces like those of [electrically charged](./Electric_charge "Electric charge") particles based on [Coulomb's law](./Coulomb's_law "Coulomb's law") are used to separate all pairs of nodes. In [equilibrium states](./Mechanical_equilibrium "Mechanical equilibrium") for this system of forces, the edges tend to have uniform length (because of the spring forces), and nodes that are not connected by an edge tend to be drawn further apart (because of the electrical repulsion). Edge attraction and vertex repulsion forces may be defined using functions that are not based on the physical behavior of springs and particles; for instance, some force-directed systems use springs whose attractive force is logarithmic rather than linear.

An alternative model considers a spring-like force for every pair of nodes 



(
i
,
j
)
{\displaystyle (i,j)}
![{\displaystyle (i,j)}](https://wikimedia.org/api/rest_v1/media/math/render/svg/8ef21910f980c6fca2b15bee102a7a0d861ed712) where the ideal length 




δ

i
j
{\displaystyle \delta \_{ij}}
![{\displaystyle \delta _{ij}}](https://wikimedia.org/api/rest_v1/media/math/render/svg/fa75d04c11480d976e1396951e02cbb3c4f71568) of each spring is proportional to the graph-theoretic distance between nodes *i* and *j*, without using a separate repulsive force. Minimizing the difference (usually the squared difference) between [Euclidean](./Euclidean_distance "Euclidean distance") and ideal distances between nodes is then equivalent to a metric [multidimensional scaling](./Multidimensional_scaling "Multidimensional scaling") problem.

A force-directed graph can involve forces other than mechanical springs and electrical repulsion. A force analogous to gravity may be used to pull vertices towards a fixed point of the drawing space; this may be used to pull together different [connected components](./Connected_component_(graph_theory) "Connected component (graph theory)") of a disconnected graph, which would otherwise tend to fly apart from each other because of the repulsive forces, and to draw nodes with greater [centrality](./Centrality "Centrality") to more central positions in the drawing;[[3]](./Force-directed_graph_drawing#cite_note-3) it may also affect the vertex spacing within a single component. Analogues of [magnetic fields](./Magnetic_field "Magnetic field") may be used for directed graphs. Repulsive forces may be placed on edges as well as on nodes in order to avoid overlap or near-overlap in the final drawing. In drawings with curved edges such as [circular arcs](./Circular_arc "Circular arc") or [spline curves](./Spline_curve "Spline curve"), forces may also be placed on the control points of these curves, for instance to improve their [angular resolution](./Angular_resolution_(graph_drawing) "Angular resolution (graph drawing)").[[4]](./Force-directed_graph_drawing#cite_note-4)

## Methods

Once the forces on the nodes and edges of a graph have been defined, the behavior of the entire graph under these sources may then be simulated as if it were a [physical system](./Physical_system "Physical system"). In such a simulation, the forces are applied to the nodes, pulling them closer together or pushing them further apart. This is repeated iteratively until the system comes to a [mechanical equilibrium](./Mechanical_equilibrium "Mechanical equilibrium") state; i.e., their relative positions do not change anymore from one iteration to the next. The positions of the nodes in this equilibrium are used to generate a drawing of the graph.

For forces defined from springs whose ideal length is proportional to the graph-theoretic distance, [stress majorization](./Stress_majorization "Stress majorization") gives a very well-behaved (i.e., monotonically [convergent](./Limit_of_a_sequence "Limit of a sequence"))[[5]](./Force-directed_graph_drawing#cite_note-dl88-5) and mathematically elegant way to [minimize](./Optimization_(mathematics) "Optimization (mathematics)") these differences and, hence, find a good layout for the graph.

It is also possible to employ mechanisms that search more directly for energy minima, either instead of or in conjunction with physical simulation. Such mechanisms, which are examples of general [global optimization](./Global_optimization "Global optimization") methods, include [simulated annealing](./Simulated_annealing "Simulated annealing") and [genetic algorithms](./Genetic_algorithm "Genetic algorithm").

## Advantages

The following are among the most important advantages of force-directed algorithms:

Good-quality results
:   At least for graphs of medium size (up to 50–500 vertices), the results obtained have usually very good quality based on the following criteria: uniform edge length, uniform vertex distribution and showing symmetry. This last criterion is among the most important ones and is hard to achieve with any other type of algorithm.

Flexibility
:   Force-directed algorithms can be easily adapted and extended to fulfill additional aesthetic criteria. This makes them the most versatile class of graph drawing algorithms. Examples of existing extensions include the ones for directed graphs, 3D graph drawing,[[6]](./Force-directed_graph_drawing#cite_note-6) cluster graph drawing, constrained graph drawing, and dynamic graph drawing.

Intuitive
:   Since they are based on physical analogies of common objects, like springs, the behavior of the algorithms is relatively easy to predict and understand. This is not the case with other types of [graph-drawing](./Graph_drawing "Graph drawing") algorithms.

Simplicity
:   Typical force-directed algorithms are simple and can be implemented in a few lines of code. Other classes of graph-drawing algorithms, like the ones for orthogonal layouts, are usually much more involved.

Interactivity
:   Another advantage of this class of algorithm is the interactive aspect. By drawing the intermediate stages of the graph, the user can follow how the graph evolves, seeing it unfold from a tangled mess into a good-looking configuration. In some interactive graph drawing tools, the user can pull one or more nodes out of their equilibrium state and watch them migrate back into position. This makes them a preferred choice for dynamic and [online](./Online_algorithm "Online algorithm") graph-drawing systems.

Strong theoretical foundations
:   While simple *ad-hoc* force-directed algorithms often appear in the literature and in practice (because they are relatively easy to understand), more reasoned approaches are starting to gain traction. Statisticians have been solving similar problems in [multidimensional scaling](./Multidimensional_scaling "Multidimensional scaling") (MDS) since the 1930s, and physicists also have a long history of working with related [n-body](./N-body "N-body") problems - so extremely mature approaches exist. As an example, the [stress majorization](./Stress_majorization "Stress majorization") approach to metric MDS can be applied to graph drawing as described above. This has been proven to [converge monotonically](./Monotone_convergence_theorem "Monotone convergence theorem").[[5]](./Force-directed_graph_drawing#cite_note-dl88-5) Monotonic convergence, the property that the algorithm will at each iteration decrease the stress or cost of the layout, is important because it guarantees that the layout will eventually reach a local minimum and stop. Damping schedules cause the algorithm to stop, but cannot guarantee that a true local minimum is reached.

## Disadvantages

The main disadvantages of force-directed algorithms include the following:

High [running time](./Time_complexity "Time complexity")
:   The typical force-directed algorithms are in general *considered* to run in cubic time (



    O
    (

    n

    3
    )
    {\displaystyle O(n^{3})}
    ![{\displaystyle O(n^{3})}](https://wikimedia.org/api/rest_v1/media/math/render/svg/6b04f5c5cfea38f43406d9442387ad28555e2609)), where 



    n
    {\displaystyle n}
    ![{\displaystyle n}](https://wikimedia.org/api/rest_v1/media/math/render/svg/a601995d55609f2d9f5e233e36fbe9ea26011b3b) is the number of nodes of the input graph. This is because the number of iterations is estimated to be linear (



    O
    (
    n
    )
    {\displaystyle O(n)}
    ![{\displaystyle O(n)}](https://wikimedia.org/api/rest_v1/media/math/render/svg/34109fe397fdcff370079185bfdb65826cb5565a)), and in every iteration, all pairs of nodes need to be visited and their mutual repulsive forces computed. This is related to the [N-body problem](./N-body_problem "N-body problem") in physics. However, since repulsive forces are local in nature the graph can be partitioned such that only neighboring vertices are considered. Common techniques used by algorithms for determining the layout of large graphs include high-dimensional embedding,[[7]](./Force-directed_graph_drawing#cite_note-7) multi-layer drawing and other methods related to [N-body simulation](./N-body_simulation "N-body simulation"). For example, the [Barnes–Hut simulation](./Barnes–Hut_simulation "Barnes–Hut simulation")-based method FADE[[8]](./Force-directed_graph_drawing#cite_note-quigley+eades-8) can improve the running time to be linearithmic, or 



    n
    log
    ⁡
    (
    n
    )
    {\displaystyle n\log(n)}
    ![{\displaystyle n\log(n)}](https://wikimedia.org/api/rest_v1/media/math/render/svg/aca080dee55ba2825a2d955bd6ca43c0e7ed04db) per iteration. As a rough guide, in a few seconds one can expect to draw at most 1,000 nodes with a standard 




    n

    2
    {\displaystyle n^{2}}
    ![{\displaystyle n^{2}}](https://wikimedia.org/api/rest_v1/media/math/render/svg/ac9810bbdafe4a6a8061338db0f74e25b7952620) per iteration technique, and 100,000 with a 



    n
    log
    ⁡
    (
    n
    )
    {\displaystyle n\log(n)}
    ![{\displaystyle n\log(n)}](https://wikimedia.org/api/rest_v1/media/math/render/svg/aca080dee55ba2825a2d955bd6ca43c0e7ed04db) per iteration technique.[[8]](./Force-directed_graph_drawing#cite_note-quigley+eades-8) Force-directed algorithms, when combined with a graph clustering approach, can draw graphs of millions of nodes.[[9]](./Force-directed_graph_drawing#cite_note-9)

Poor local minima
:   It is easy to see that force-directed algorithms produce a graph with minimal energy, in particular one whose total energy is only a [local minimum](./Local_minimum "Local minimum"). The local minimum found can be, in many cases, considerably worse than a global minimum, which translates into a low-quality drawing. For many algorithms, especially the ones that allow only *down-hill* moves of the vertices, the final result can be strongly influenced by the initial layout, that in most cases is randomly generated. The problem of poor local minima becomes more important as the number of vertices of the graph increases. A combined application of different algorithms is helpful to solve this problem.[[10]](./Force-directed_graph_drawing#cite_note-10) For example, using the Kamada–Kawai algorithm[[11]](./Force-directed_graph_drawing#cite_note-kk89-11) to quickly generate a reasonable initial layout and then the Fruchterman–Reingold algorithm[[12]](./Force-directed_graph_drawing#cite_note-fr91-12) to improve the placement of neighbouring nodes. Another technique to achieve a global minimum is to use a multilevel approach.[[13]](./Force-directed_graph_drawing#cite_note-13)

## History

Force-directed methods in graph drawing date back to the work of [Tutte (1963)](./Force-directed_graph_drawing#CITEREFTutte1963), who showed that [polyhedral graphs](./Polyhedral_graph "Polyhedral graph") may be drawn in the plane with all faces convex by fixing the vertices of the outer face of a planar embedding of the graph into [convex position](./Convex_position "Convex position"), placing a spring-like attractive force on each edge, and letting the system settle into an equilibrium.[[14]](./Force-directed_graph_drawing#cite_note-14) Because of the simple nature of the forces in this case, the system cannot get stuck in local minima, but rather converges to a unique global optimum configuration. Because of this work, embeddings of planar graphs with convex faces are sometimes called [Tutte embeddings](./Tutte_embedding "Tutte embedding").

The combination of attractive forces on adjacent vertices, and repulsive forces on all vertices, was first used by [Eades (1984)](./Force-directed_graph_drawing#CITEREFEades1984);[[15]](./Force-directed_graph_drawing#cite_note-15) additional pioneering work on this type of force-directed layout was done by [Fruchterman & Reingold (1991)](./Force-directed_graph_drawing#CITEREFFruchtermanReingold1991).[[12]](./Force-directed_graph_drawing#cite_note-fr91-12) The idea of using only spring forces between all pairs of vertices, with ideal spring lengths equal to the vertices' graph-theoretic distance, is from [Kamada & Kawai (1989)](./Force-directed_graph_drawing#CITEREFKamadaKawai1989).[[11]](./Force-directed_graph_drawing#cite_note-kk89-11)

## See also

- [Cytoscape](./Cytoscape "Cytoscape"), software for visualising biological networks. The base package includes force-directed layouts as one of the built-in methods.
- [Gephi](./Gephi "Gephi"), an interactive visualization and exploration platform for all kinds of networks and complex systems, dynamic and hierarchical graphs.
- [Graphviz](./Graphviz "Graphviz"), software that implements a multilevel force-directed layout algorithm (among many others) capable of handling very large graphs.
- [Tulip](./Tulip_(software) "Tulip (software)"), software that implements most of the force-directed layout algorithms (GEM, LGL, GRIP, FM³).
- [Prefuse](./Prefuse "Prefuse")

## References

1. [↑](./Force-directed_graph_drawing#cite_ref-1) Grandjean, Martin (2015), "Introduction à la visualisation de données, l'analyse de réseau en histoire", [*Geschichte und Informatik 18/19*](http://www.martingrandjean.ch/wp-content/uploads/2015/09/Grandjean2015.pdf) (PDF), pp. 109–128
2. [↑](./Force-directed_graph_drawing#cite_ref-2) Kobourov, Stephen G. (2012), *Spring Embedders and Force-Directed Graph Drawing Algorithms*, [arXiv](./ArXiv_(identifier) "ArXiv (identifier)"):[1201.3011](https://arxiv.org/abs/1201.3011), [Bibcode](./Bibcode_(identifier) "Bibcode (identifier)"):[2012arXiv1201.3011K](https://ui.adsabs.harvard.edu/abs/2012arXiv1201.3011K).
3. [↑](./Force-directed_graph_drawing#cite_ref-3) Bannister, M. J.; [Eppstein, D.](./David_Eppstein "David Eppstein"); [Goodrich, M. T.](./Michael_T._Goodrich "Michael T. Goodrich"); Trott, L. (2012), "Force-directed graph drawing using social gravity and scaling", *Proc. 20th Int. Symp. Graph Drawing*, [arXiv](./ArXiv_(identifier) "ArXiv (identifier)"):[1209.0748](https://arxiv.org/abs/1209.0748), [Bibcode](./Bibcode_(identifier) "Bibcode (identifier)"):[2012arXiv1209.0748B](https://ui.adsabs.harvard.edu/abs/2012arXiv1209.0748B).
4. [↑](./Force-directed_graph_drawing#cite_ref-4) Chernobelskiy, R.; Cunningham, K.; [Goodrich, M. T.](./Michael_T._Goodrich "Michael T. Goodrich"); Kobourov, S. G.; Trott, L. (2011), "Force-directed Lombardi-style graph drawing", [*Proc. 19th Symposium on Graph Drawing*](http://www.cs.arizona.edu/~kobourov/fdl.pdf) (PDF), pp. 78–90.
5. [1](./Force-directed_graph_drawing#cite_ref-dl88_5-0) [2](./Force-directed_graph_drawing#cite_ref-dl88_5-1) de Leeuw, Jan (1988), "Convergence of the majorization method for multidimensional scaling", *Journal of Classification*, **5** (2), Springer: 163–180, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1007/BF01897162](https://doi.org/10.1007%2FBF01897162), [S2CID](./S2CID_(identifier) "S2CID (identifier)") [122413124](https://api.semanticscholar.org/CorpusID:122413124).
6. [↑](./Force-directed_graph_drawing#cite_ref-6) Vose, Aaron, [*3D Phylogenetic Tree Viewer*](http://aaronvose.net/phytree3d/), retrieved 3 June 2012
7. [↑](./Force-directed_graph_drawing#cite_ref-7) [Harel, David](./David_Harel "David Harel"); Koren, Yehuda (2002), "Graph drawing by high-dimensional embedding", *Proceedings of the 9th International Symposium on Graph Drawing*, Springer, pp. 207–219, [CiteSeerX](./CiteSeerX_(identifier) "CiteSeerX (identifier)") [10.1.1.20.5390](https://citeseerx.ist.psu.edu/viewdoc/summary?doi=10.1.1.20.5390), [ISBN](./ISBN_(identifier) "ISBN (identifier)") [3-540-00158-1](./Special:BookSources/3-540-00158-1 "Special:BookSources/3-540-00158-1") `{{citation}}`: Cite uses deprecated parameter `|citeseerx=` ([help](./Help:CS1_errors#deprecated_params "Help:CS1 errors"))
8. [1](./Force-directed_graph_drawing#cite_ref-quigley+eades_8-0) [2](./Force-directed_graph_drawing#cite_ref-quigley+eades_8-1) Quigley, Aaron; [Eades, Peter](./Peter_Eades "Peter Eades") (2001), "FADE: Graph Drawing, Clustering, and Visual Abstraction", [*Proceedings of the 8th International Symposium on Graph Drawing*](https://aaronquigley.org/wp-content/uploads/2019/03/Fade-2000-aquigley.pdf) (PDF), pp. 197–210, [ISBN](./ISBN_(identifier) "ISBN (identifier)") [3-540-41554-8](./Special:BookSources/3-540-41554-8 "Special:BookSources/3-540-41554-8").
9. [↑](./Force-directed_graph_drawing#cite_ref-9) [*A Gallery of Large Graphs*](http://yifanhu.net/GALLERY/GRAPHS/), retrieved 22 Oct 2017
10. [↑](./Force-directed_graph_drawing#cite_ref-10) Collberg, Christian; Kobourov, Stephen; Nagra, Jasvir; Pitts, Jacob; Wampler, Kevin (2003), "A System for Graph-based Visualization of the Evolution of Software", [*Proceedings of the 2003 ACM Symposium on Software Visualization (SoftVis '03)*](https://www.researchgate.net/publication/2851716), New York, NY, USA: ACM, pp. 77–86, figures on p. 212, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1145/774833.774844](https://doi.org/10.1145%2F774833.774844), [ISBN](./ISBN_(identifier) "ISBN (identifier)") [1-58113-642-0](./Special:BookSources/1-58113-642-0 "Special:BookSources/1-58113-642-0"), [S2CID](./S2CID_(identifier) "S2CID (identifier)") [824991](https://api.semanticscholar.org/CorpusID:824991), "To achieve an aesthetically pleasing layout of the graph it is also necessary to employ modified Fruchterman–Reingold forces, as the Kamada–Kawai method does not achieve satisfactory methods by itself but rather creates a good approximate layout so that the Fruchterman-Reingold calculations can quickly "tidy up" the layout."
11. [1](./Force-directed_graph_drawing#cite_ref-kk89_11-0) [2](./Force-directed_graph_drawing#cite_ref-kk89_11-1) Kamada, Tomihisa; Kawai, Satoru (1989), "An algorithm for drawing general undirected graphs", *Information Processing Letters*, **31** (1), Elsevier: 7–15, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1016/0020-0190(89)90102-6](https://doi.org/10.1016%2F0020-0190%2889%2990102-6).
12. [1](./Force-directed_graph_drawing#cite_ref-fr91_12-0) [2](./Force-directed_graph_drawing#cite_ref-fr91_12-1) Fruchterman, Thomas M. J.; [Reingold, Edward M.](./Edward_Reingold "Edward Reingold") (1991), "Graph Drawing by Force-Directed Placement", *Software: Practice and Experience*, **21** (11), Wiley: 1129–1164, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1002/spe.4380211102](https://doi.org/10.1002%2Fspe.4380211102), [S2CID](./S2CID_(identifier) "S2CID (identifier)") [31468174](https://api.semanticscholar.org/CorpusID:31468174).
13. [↑](./Force-directed_graph_drawing#cite_ref-13) Walshaw, Chris (2003), "A multilevel algorithm for force-directed graph-drawing", *Journal of Graph Algorithms and Applications*, **7** (3): 253–285, [doi](./Doi_(identifier) "Doi (identifier)"):[10.7155/jgaa.00070](https://doi.org/10.7155%2Fjgaa.00070), [MR](./MR_(identifier) "MR (identifier)") [2112231](https://mathscinet.ams.org/mathscinet-getitem?mr=2112231)
14. [↑](./Force-directed_graph_drawing#cite_ref-14) [Tutte, W. T.](./W._T._Tutte "W. T. Tutte") (1963), "How to draw a graph", *Proceedings of the London Mathematical Society*, **13** (52): 743–768, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1112/plms/s3-13.1.743](https://doi.org/10.1112%2Fplms%2Fs3-13.1.743).
15. [↑](./Force-directed_graph_drawing#cite_ref-15) [Eades, Peter](./Peter_Eades "Peter Eades") (1984), "A Heuristic for Graph Drawing", *Congressus Numerantium*, **42** (11): 149–160.

## Further reading

- di Battista, Giuseppe; [Peter Eades](./Peter_Eades "Peter Eades"); [Roberto Tamassia](./Roberto_Tamassia "Roberto Tamassia"); Ioannis G. Tollis (1999), *Graph Drawing: Algorithms for the Visualization of Graphs*, Prentice Hall, [ISBN](./ISBN_(identifier) "ISBN (identifier)") [978-0-13-301615-4](./Special:BookSources/978-0-13-301615-4 "Special:BookSources/978-0-13-301615-4")
- Kaufmann, Michael; [Wagner, Dorothea](./Dorothea_Wagner "Dorothea Wagner"), eds. (2001), *Drawing graphs: methods and models*, Lecture Notes in Computer Science 2025, vol. 2025, Springer, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1007/3-540-44969-8](https://doi.org/10.1007%2F3-540-44969-8), [ISBN](./ISBN_(identifier) "ISBN (identifier)") [978-3-540-42062-0](./Special:BookSources/978-3-540-42062-0 "Special:BookSources/978-3-540-42062-0"), [S2CID](./S2CID_(identifier) "S2CID (identifier)") [1808286](https://api.semanticscholar.org/CorpusID:1808286)

## External links

- [Book chapter on Force-Directed Drawing Algorithms](https://cs.brown.edu/people/rtamassi/gdhandbook/chapters/force-directed.pdf) by Stephen G. Kobourov