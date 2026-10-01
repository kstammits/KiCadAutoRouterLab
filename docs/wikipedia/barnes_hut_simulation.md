Barnes–Hut simulation

# Barnes–Hut simulation

Approximation algorithm for the n-body problem

---

The **Barnes–Hut simulation** (named after Joshua Barnes and [Piet Hut](./Piet_Hut "Piet Hut")) is an [approximation algorithm](./Approximation_algorithm "Approximation algorithm") for performing an [N-body simulation](./N-body_simulation "N-body simulation"). It is notable for having [order](./Big_O_notation "Big O notation") O(*n* log *n*) compared to a direct-sum algorithm which would be O(*n*2).[[1]](./Barnes–Hut_simulation#cite_note-1)



A 100-body simulation with the Barnes–Hut tree visually as blue boxes.

The simulation volume is usually divided up into cubic cells via an [octree](./Octree "Octree") (in a three-dimensional space), so that only [particles](./Point_particle "Point particle") from nearby cells need to be treated individually, and particles in distant cells can be treated as a single large particle centered at the cell's [center of mass](./Center_of_mass "Center of mass") (or as a low-order [multipole expansion](./Multipole_expansion "Multipole expansion")). This can dramatically reduce the number of particle pair interactions that must be computed.

Dynamic visualization of the quadtree structure of the Barnes-Hut algorithm for the 2D N-body problem

Some of the most demanding [high-performance computing](./High-performance_computing "High-performance computing") projects perform [computational astrophysics](./Computational_astrophysics "Computational astrophysics") using the Barnes–Hut treecode algorithm, such as [DEGIMA](./DEGIMA "DEGIMA").[[2]](./Barnes–Hut_simulation#cite_note-2)[*[citation needed](./Wikipedia:Citation_needed "Wikipedia:Citation needed")*]

## Algorithm

### The Barnes–Hut tree

In a three-dimensional [*N*-body simulation](./N-body_simulation "N-body simulation"), the Barnes–Hut algorithm [recursively](./Recursion_(computer_science) "Recursion (computer science)") divides the *n* bodies into groups by storing them in an [octree](./Octree "Octree") (or a [quad-tree](./Quadtree "Quadtree") in a 2D simulation). Each [node](./Node_(graph_theory) "Node (graph theory)") in this tree represents a region of the three-dimensional space.
The topmost node represents the whole space, and its eight children represent the eight [octants](./Cartesian_coordinate_system#Quadrants_and_octants "Cartesian coordinate system") of the space. The space is recursively subdivided into octants until each subdivision contains 0 or 1 bodies (some regions do not have bodies in all of their octants).
There are two types of nodes in the octree: internal and external nodes. An external node has no children and is either empty or represents a single body. Each internal node represents the group of bodies beneath it, and stores the [center of mass](./Center_of_mass "Center of mass") and the total mass of all its children bodies.

- Particle distribution resembling two neighboring galaxies.
- Complete Barnes–Hut tree. (Nodes that do not contain particles are not drawn)
- Nodes of the Barnes–Hut tree used for calculating the force acting on a particle at the point of origin.
- [![](//thumb.wikimedia.org/wikipedia/commons/thumb/2/25/Galaxy_collision.ogv/120px--Galaxy_collision.ogv.jpg?utm_source=en.wikipedia.org&utm_campaign=parser)](//upload.wikimedia.org/wikipedia/commons/2/25/Galaxy_collision.ogv?utm_source=en.wikipedia.org&utm_campaign=index&utm_content=original)

  N-body simulation based on the Barnes–Hut algorithm.

### Calculating the force acting on a body

To calculate the [net force](./Net_force "Net force") on a particular body, the nodes of the tree are traversed, starting from the root. If the center of mass of an internal node is sufficiently far from the body, the bodies contained in that part of the tree are treated as a single particle whose position and mass is respectively the center of mass and total mass of the internal node. If the internal node is sufficiently close to the body, the process is repeated for each of its children.

Whether a node is or isn't sufficiently far away from a body, depends on the quotient 



s

/
d
{\displaystyle s/d}
![{\displaystyle s/d}](//wikimedia.org/api/rest_v1/media/math/render/svg/e303cf28e76a7af4231202c4d325ba0ec8b3ce32), where *s* is the width of the region represented by the internal node, and *d* is the distance between the body and the node's center of mass. The node is sufficiently far away when this ratio is smaller than a threshold value *θ*. The parameter *θ* determines the accuracy of the simulation; larger values of *θ* increase the speed of the simulation but decreases its accuracy. If *θ* = 0, no internal node is treated as a single body and the algorithm degenerates to a direct-sum algorithm.

## See also

- [NEMO (Stellar Dynamics Toolbox)](./NEMO_(Stellar_Dynamics_Toolbox) "NEMO (Stellar Dynamics Toolbox)")
- [Nearest neighbor search](./Nearest_neighbor_search "Nearest neighbor search")
- [Fast multipole method](./Fast_multipole_method "Fast multipole method")

---

## References and sources

References

1. [[1]](./Barnes–Hut_simulation#pcs-ref-back-link-cite_note-1)

   Pfalzner, Susanne; Gibbon, Paul (1996). [*Many-body tree methods in physics*](//archive.org/details/manybodytreemeth00pfal_902). Cambridge [u.a.]: [Cambridge Univ. Press](./Cambridge_Univ._Press "Cambridge Univ. Press"). pp. [2](//archive.org/details/manybodytreemeth00pfal_902/page/n9), 3. [ISBN](./ISBN_(identifier) "ISBN (identifier)") [978-0-521-49564-6](./Special:BookSources/978-0-521-49564-6 "Special:BookSources/978-0-521-49564-6").
2. [[2]](./Barnes–Hut_simulation#pcs-ref-back-link-cite_note-2)

   Hamada, Tsuyoshi; Nitadori, Keigo; Benkrid, Khaled; Ohno, Yousuke; Morimoto, Gentaro; Masada, Tomonari; Shibata, Yuichiro; Oguri, Kiyoshi; Taiji, Makoto (2009). ["A novel multiple-walk parallel algorithm for the Barnes–Hut treecode on GPUs – towards cost effective, high performance N-body simulation"](http://link.springer.com/10.1007/s00450-009-0089-1). *Computer Science - Research and Development*. **24** (1–2): 21–31. [doi](./Doi_(identifier) "Doi (identifier)"):[10.1007/s00450-009-0089-1](//doi.org/10.1007%2Fs00450-009-0089-1). [ISSN](./ISSN_(identifier) "ISSN (identifier)") [1865-2034](//search.worldcat.org/issn/1865-2034). [S2CID](./S2CID_(identifier) "S2CID (identifier)") [31071570](//api.semanticscholar.org/CorpusID:31071570).

Sources

- J. Barnes & P. Hut (December 1986). "A hierarchical O(*N* log *N*) force-calculation algorithm". *Nature*. **324** (4): 446–449. [Bibcode](./Bibcode_(identifier) "Bibcode (identifier)"):[1986Natur.324..446B](//ui.adsabs.harvard.edu/abs/1986Natur.324..446B). [doi](./Doi_(identifier) "Doi (identifier)"):[10.1038/324446a0](//doi.org/10.1038%2F324446a0). [S2CID](./S2CID_(identifier) "S2CID (identifier)") [4267861](//api.semanticscholar.org/CorpusID:4267861).
- J. Dubinski (October 1996). "A Parallel Tree Code". *New Astronomy*. **1** (2): 133–147. [arXiv](./ArXiv_(identifier) "ArXiv (identifier)"):[astro-ph/9603097v1](//arxiv.org/abs/astro-ph/9603097v1). [Bibcode](./Bibcode_(identifier) "Bibcode (identifier)"):[1996NewA....1..133D](//ui.adsabs.harvard.edu/abs/1996NewA....1..133D). [doi](./Doi_(identifier) "Doi (identifier)"):[10.1016/S1384-1076(96)00009-7](//doi.org/10.1016%2FS1384-1076%2896%2900009-7). [S2CID](./S2CID_(identifier) "S2CID (identifier)") [119464486](//api.semanticscholar.org/CorpusID:119464486).
- U. Becciani; R. Ansalonib; V. Antonuccio-Delogua; G. Erbaccic; M. Gamberaa & A. Pagliarod (October 1997). "A parallel tree code for large *N*-body simulation: dynamic load balance and data distribution on a CRAY T3D system". *Computer Physics Communications*. **106** (1–2): 105–113. [arXiv](./ArXiv_(identifier) "ArXiv (identifier)"):[physics/9709003](//arxiv.org/abs/physics/9709003). [Bibcode](./Bibcode_(identifier) "Bibcode (identifier)"):[1997CoPhC.106..105B](//ui.adsabs.harvard.edu/abs/1997CoPhC.106..105B). [doi](./Doi_(identifier) "Doi (identifier)"):[10.1016/S0010-4655(97)00102-1](//doi.org/10.1016%2FS0010-4655%2897%2900102-1). [S2CID](./S2CID_(identifier) "S2CID (identifier)") [18428101](//api.semanticscholar.org/CorpusID:18428101).
- T. Ventimiglia & K. Wayne. ["The Barnes–Hut Algorithm"](http://arborjs.org/docs/barnes-hut). Retrieved 30 March 2012.

## External links

- [Treecodes, J. Barnes](http://ifa.hawaii.edu/~barnes/software.html)
- [Parallel TreeCode](http://www.cita.utoronto.ca/~dubinski/treecode/treecode.html) [Archived](//web.archive.org/web/20130402125257/http://www.cita.utoronto.ca/~dubinski/treecode/treecode.html) 2013-04-02 at the [Wayback Machine](./Wayback_Machine "Wayback Machine")
- [HTML5/JavaScript Example Graphical Barnes–Hut Simulation](//web.archive.org/web/20140413142523/http://www.andrew.cmu.edu/user/sameera/demos/BNtree/)
- [PEPC – The Pretty Efficient Parallel Coulomb solver](http://www.fz-juelich.de/ias/jsc/pepc), an open-source parallel Barnes–Hut tree code with exchangeable interaction kernel for a multitude of applications
- [Parallel GPU N-body simulation program with fast stackless particles tree traversal](//github.com/drons/nbody)
- at beltoforion.de