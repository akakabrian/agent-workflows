import FormalConjectures.WrittenOnTheWallII.GraphConjecture146
import FormalConjecturesForMathlib.WrittenOnTheWallII.GraphConjecture142Proof

open Classical
open SimpleGraph

/-! Standalone, CI-checked proof harness for Written on the Wall II Conjecture 146. -/

#check WrittenOnTheWallII.GraphConjecture146.conjecture146
#check WrittenOnTheWallII.GraphConjecture146.graphSquareRadius
#check graphSquare
#check eccSet
#check maxEccentricityVertices
#check largestInducedTreeSize
#check SimpleGraph.diam_add_one_le_largestInducedTreeSize_splice
#check SimpleGraph.eccSet_maxEccentricityVertices_add_one_le_diam_splice
#check SimpleGraph.maxEccentricityVertices_nonempty_splice
#check SimpleGraph.exists_eccSet_witness_splice
#check SimpleGraph.IsTree.induce_insert_of_unique_adj
#check SimpleGraph.exists_dist_eq_diam
#check SimpleGraph.exists_eccent_eq_radius
#check SimpleGraph.ediam_le_two_mul_radius
#check SimpleGraph.radius_ne_top_iff
#check SimpleGraph.Connected.exists_walk_length_eq_dist
#check SimpleGraph.exists_walk_of_dist_ne_zero
#check SimpleGraph.dist_le
#check SimpleGraph.dist_eq_one_iff_adj
#check SimpleGraph.Walk.isPath_iff_dist_eq_length
#check SimpleGraph.Walk.IsPath
#check SimpleGraph.Walk.support
#check SimpleGraph.Walk.support_toFinset_card
