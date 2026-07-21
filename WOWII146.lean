import FormalConjectures.WrittenOnTheWallII.GraphConjecture146
import FormalConjecturesForMathlib.WrittenOnTheWallII.GraphConjecture142Proof

/-! Standalone, CI-checked proof of Written on the Wall II Conjecture 146. -/

open Classical
open SimpleGraph

#check WrittenOnTheWallII.GraphConjecture146.conjecture146
#check WrittenOnTheWallII.GraphConjecture146.graphSquareRadius
#check SimpleGraph.diam_add_one_le_largestInducedTreeSize_splice
#check SimpleGraph.eccSet_maxEccentricityVertices_add_one_le_diam_splice
#check SimpleGraph.maxEccentricityVertices_nonempty_splice
#check SimpleGraph.exists_eccSet_witness_splice
#check SimpleGraph.IsTree.induce_insert_of_unique_adj
#check SimpleGraph.distToSet_le_dist_of_mem_public
#check SimpleGraph.connected_iff_ediam_ne_top
#check SimpleGraph.Connected.mono
#check SimpleGraph.Preconnected.mono
#check SimpleGraph.Connected.pos_dist_of_ne
#check SimpleGraph.Connected.dist_triangle
#check SimpleGraph.Connected.coe_dist_eq_edist
#check SimpleGraph.Preconnected.coe_dist_eq_edist
#check SimpleGraph.edist_le_eccent
#check SimpleGraph.eccent_le_ediam
#check SimpleGraph.radius_le_eccent
#check SimpleGraph.exists_eccent_eq_radius
#check SimpleGraph.radius_ne_top_iff
#check ENat.toNat_le_toNat
#check ENat.coe_toNat
#check ENat.coe_toNat_eq_self
#check ENat.toNat_eq_iff
#check SimpleGraph.dist_eq_one_iff_adj
#check SimpleGraph.dist_eq_zero_iff_eq_or_not_reachable
#check SimpleGraph.IsTree.induce_singleton
#check SimpleGraph.isTree_induce_singleton
#check SimpleGraph.IsTree.singleton
#check SimpleGraph.Walk.IsPath.isTree_induce_support
#check SimpleGraph.Walk.IsPath.induce_support_isTree
#check SimpleGraph.Walk.IsPath.induce_support_toFinset_isTree
#check SimpleGraph.finset_card_le_largestInducedTreeSize_splice
#check SimpleGraph.card_le_largestInducedTreeSize_splice
#check Nat.le_csSup
#check Nat.le_sSup
