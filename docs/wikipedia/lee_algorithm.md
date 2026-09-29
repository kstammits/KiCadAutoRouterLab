Lee algorithm

Algorithm based on breadth-first search to solve mazes



Main article: [Routing (electronic design automation)](./Routing_(electronic_design_automation) "Routing (electronic design automation)")

[![](//thumb.wikimedia.org/wikipedia/commons/thumb/5/5a/Lee_waveprop.png/250px-Lee_waveprop.png?utm_source=en.wikipedia.org&utm_campaign=parser&utm_content=thumbnail)](./File:Lee_waveprop.png)

Wave Expansion step

The **Lee algorithm** is one possible solution for [maze routing problems](./Maze_router "Maze router") based on [breadth-first search](./Breadth-first_search "Breadth-first search").
It always gives an optimal solution, if one exists, but is slow and requires considerable memory.

## Algorithm

*Initialization*

:   Select start point, mark with 0
:   i := 0

*Wave expansion*

:   REPEAT

    :   Mark all unlabeled neighbors of points marked with i with i+1
    :   i := i+1
:   UNTIL ((target reached) or (no points can be marked))

*Backtrace*

:   go to the target point
:   REPEAT

    :   go to next node that has a lower mark than the current node
    :   add this node to path
:   UNTIL (start point reached)

*Clearance*

:   Block the path for future wirings
:   Delete all marks

Of course the wave expansion marks only points in the routable area of the chip, not in the blocks or already wired parts, and to minimize segmentation you should keep in one direction as long as possible.

## External links

- <http://www.eecs.northwestern.edu/~haizhou/357/lec6.pdf>

## References

- Wolf, Wayne (2002), *Modern VLSI Design*, Prentice Hall, pp. 518ff, [ISBN](./ISBN_(identifier) "ISBN (identifier)") [0-13-061970-1](./Special:BookSources/0-13-061970-1 "Special:BookSources/0-13-061970-1")
- Lee, C. Y. (1961), "An Algorithm for Path Connections and Its Applications", *IRE Transactions on Electronic Computers*, EC-10 (3): 346–365, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1109/TEC.1961.5219222](https://doi.org/10.1109%2FTEC.1961.5219222), [S2CID](./S2CID_(identifier) "S2CID (identifier)") [40700386](https://api.semanticscholar.org/CorpusID:40700386)
- Rubin, F (1974), "The Lee Path Connection Algorithm", *IRE Transactions on Electronic Computers*, **C-23** (9): 907–914, [doi](./Doi_(identifier) "Doi (identifier)"):[10.1109/T-C.1974.224054](https://doi.org/10.1109%2FT-C.1974.224054), [S2CID](./S2CID_(identifier) "S2CID (identifier)") [32651989](https://api.semanticscholar.org/CorpusID:32651989)





Remzi Osmanli

|  |  |
| --- | --- |
| [Stub icon](./File:Printed_circuit_board_PCB_icon_3.svg) | This electronics-related article is a [stub](./Wikipedia:Stub "Wikipedia:Stub"). You can help Wikipedia by [adding missing information](https://en.wikipedia.org/w/index.php?title=Lee_algorithm&action=edit). |

- [v](./Template:Electronics-stub "Template:Electronics-stub")
- [t](./Template_talk:Electronics-stub "Template talk:Electronics-stub")
- [e](./Special:EditPage/Template:Electronics-stub "Special:EditPage/Template:Electronics-stub")