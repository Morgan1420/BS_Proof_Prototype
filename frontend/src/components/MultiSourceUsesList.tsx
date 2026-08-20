/**
 * @deprecated Phase 24 — superseded by `ScientificConclusionsList.tsx` in
 * this same directory. This file is left in place (not deleted/renamed)
 * per this codebase's "deprecate, don't delete" convention for retired
 * modules, and because destructive file operations require explicit user
 * action rather than being run automatically. It is not imported or
 * rendered anywhere in the app — `IngredientCard.tsx` now imports
 * `ScientificConclusionsList` instead (see that file's Scientific
 * Information section). Do not import from this file; do not add new
 * features here.
 *
 * This component's original type import was `MultiSourceRecommendedUse`
 * (`../services/api`), which was itself renamed to `ScientificConclusion`
 * as part of the Phase 24 terminology rename (`recommended_uses` ->
 * `scientific_conclusions`) — that old type name no longer exists in
 * `api.ts`. The import below has been updated to the renamed
 * `ScientificConclusion` type purely so this retired file still type-checks
 * (`tsc --noEmit` covers every file in the project regardless of whether
 * it's actually imported), NOT because this component was otherwise
 * touched — its props/internals below still use the old
 * `recommendedUses`/"Multi-Source Recommended Uses" naming as a historical
 * reference for the Phase 23 implementation this was renamed from.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { View, Text, Pressable, StyleSheet, Modal, ScrollView } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import type { ScientificConclusion as MultiSourceRecommendedUse } from '../services/api';
import { isPaperGrade, sortByGradeThenScore } from '../utils/grades';
import Pagination from './Pagination';
import CollapsibleSection from './CollapsibleSection';
import GradeCircleBadge from './GradeCircleBadge';

/** Max items shown per page — same "5 per list page" rule every other
 * Scientific Information list panel uses (see StudiesList.tsx/
 * RecommendedUsesList.tsx/VerifiedResourcesList.tsx's own PAGE_SIZE
 * constants). */
const PAGE_SIZE = 5;

/**
 * "Multi-Source Recommended Uses" — Phase 23. Renders
 * `Ingredient.recommended_uses` (see MultiSourceRecommendedUse's own
 * doc-comment in services/api.ts), each claim scored against the
 * four-category Multi-Source Confidence Rubric
 * (docs/multi_source_confidence_rubric.json) combining BOTH
 * peer-reviewed paper evidence and official regulatory/health-authority
 * backing.
 *
 * **Deliberately a separate component from RecommendedUsesList.tsx**,
 * not an extension of it — despite the near-identical name, the two
 * render entirely different backend data (PaperConclusion rows, Phase 5,
 * vs. this component's Ingredient.recommended_uses array, Phase 11/23 —
 * see MultiSourceRecommendedUse's doc-comment for the full distinction).
 * Folding this into RecommendedUsesList.tsx would have meant branching
 * that component's entire row/modal shape on which of two incompatible
 * item types it received; a second, clearly-named component sitting
 * alongside it in IngredientCard.tsx's Scientific Information section
 * keeps both simple and independently understandable, matching this
 * codebase's established preference for calling out this kind of
 * confusing-name situation explicitly rather than silently overloading
 * one component/prop for two different things (see e.g. IngredientCard.
 * tsx's own repeated "this is a pure passthrough, the real modal lives
 * elsewhere" notes from Phases 19-22).
 *
 * Same unified list-panel chrome (CollapsibleSection, GradeCircleBadge,
 * Pagination, rubric + info modal pair) as the other three Scientific
 * Information panels, and the same "sort by grade rank then score,
 * before pagination" rule (see utils/grades.ts::sortByGradeThenScore).
 *
 * Palette note: same as the other three lists — this only ever renders
 * while its parent IngredientCard is already expanded (all-orange
 * internals), so colors are hardcoded to `colors.orange` rather than
 * conditioned on an `isExpanded` prop.
 */
export interface MultiSourceUsesListProps {
  /** Every stored MultiSourceRecommendedUse for this ingredient
   * (unfiltered, unpaginated) — see IngredientDetailResponse.
   * recommended_uses on the backend. `undefined` means "not fetched yet"
   * (renders the loading state); an empty array means "fetched, but
   * synthesis hasn't produced any specific claim yet" — both render the
   * same empty-state message, since neither case is actionable
   * differently from the user's point of view. */
  recommendedUses: MultiSourceRecommendedUse[] | undefined;
  isLoading?: boolean;
  errorMessage?: string | null;
}

const MultiSourceUsesList: React.FC<MultiSourceUsesListProps> = ({
  recommendedUses,
  isLoading = false,
  errorMessage = null,
}) => {
  const [page, setPage] = useState<number>(0);
  const [activeInfoModalItem, setActiveInfoModalItem] =
    useState<MultiSourceRecommendedUse | null>(null);
  const [activeRubricModalItem, setActiveRubricModalItem] =
    useState<MultiSourceRecommendedUse | null>(null);

  // Sorted (grade rank, then total_score descending) before pagination
  // chunking below, per the Scientific Information section's "sort
  // before paginating" rule every other list here follows. Unlike
  // RecommendedUsesList.tsx, no "C or higher" filter is applied — every
  // synthesized claim this endpoint returns is shown, since this array
  // (unlike the always-growing PaperConclusion table) is fully
  // regenerated on each synthesis run, so a low-confidence entry still
  // reflects the model's most current judgment rather than stale noise.
  const sortedUses = useMemo<MultiSourceRecommendedUse[] | undefined>(() => {
    if (!recommendedUses) {
      return undefined;
    }
    return sortByGradeThenScore(
      recommendedUses,
      (use) => use.confidence_grade,
      (use) => use.total_score
    );
  }, [recommendedUses]);

  const totalPages = sortedUses ? Math.max(1, Math.ceil(sortedUses.length / PAGE_SIZE)) : 1;

  const pageItems = useMemo<MultiSourceRecommendedUse[]>(() => {
    if (!sortedUses) {
      return [];
    }
    const start = page * PAGE_SIZE;
    return sortedUses.slice(start, start + PAGE_SIZE);
  }, [sortedUses, page]);

  // Clamp the current page if the list shrinks out from under us (e.g. a
  // fresh grade request replaces `recommendedUses` with fewer entries) —
  // same guard every other list here applies to its own pagination.
  useEffect(() => {
    setPage((current) => Math.min(current, totalPages - 1));
  }, [totalPages]);

  const totalCount = sortedUses?.length ?? 0;

  return (
    <CollapsibleSection title={`Multi-Source Recommended Uses (Total: ${totalCount})`}>
      {isLoading && !sortedUses ? (
        <Text style={styles.statusText}>Loading recommended uses...</Text>
      ) : errorMessage ? (
        <Text style={styles.statusText}>{errorMessage}</Text>
      ) : !sortedUses || sortedUses.length === 0 ? (
        <Text style={styles.statusText}>
          No multi-source recommended uses synthesized yet for this ingredient.
        </Text>
      ) : (
        <>
          <View style={styles.list}>
            {pageItems.map((use, index) => (
              <View
                key={`${use.claim}-${index}`}
                style={[styles.row, index === pageItems.length - 1 && styles.rowLast]}
              >
                <Text style={styles.claimText} numberOfLines={2}>
                  {use.claim}
                </Text>
                <View style={styles.rowActions}>
                  {isPaperGrade(use.confidence_grade) && (
                    <GradeCircleBadge
                      grade={use.confidence_grade}
                      onPress={() => setActiveRubricModalItem(use)}
                    />
                  )}
                  <Pressable
                    style={styles.iconButton}
                    onPress={() => setActiveInfoModalItem(use)}
                    accessibilityRole="button"
                    accessibilityLabel={`View details for ${use.claim}`}
                    hitSlop={6}
                  >
                    <Ionicons
                      name="information-circle-outline"
                      size={20}
                      color={colors.orange}
                    />
                  </Pressable>
                </View>
              </View>
            ))}
          </View>

          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      )}

      {/* General Info Modal — sources_summary badges, supporting
          study/resource counts, and the grade justification text. */}
      <Modal
        visible={activeInfoModalItem !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setActiveInfoModalItem(null)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setActiveInfoModalItem(null)}>
          <Pressable style={styles.modalCard} onPress={(event) => event.stopPropagation()}>
            {activeInfoModalItem && (
              <>
                <View style={styles.modalHeaderRow}>
                  <Text style={styles.modalTitle} numberOfLines={4}>
                    {activeInfoModalItem.claim}
                  </Text>
                  <Pressable
                    onPress={() => setActiveInfoModalItem(null)}
                    accessibilityRole="button"
                    accessibilityLabel="Close"
                    hitSlop={8}
                  >
                    <Ionicons name="close" size={22} color={colors.orange} />
                  </Pressable>
                </View>

                <ScrollView style={styles.modalScroll}>
                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Total Score</Text>
                    <Text style={styles.modalSectionValue}>
                      {activeInfoModalItem.total_score} / 100
                    </Text>
                  </View>

                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Source Support</Text>
                    <Text style={styles.modalSectionValue}>
                      {activeInfoModalItem.supporting_study_count} supporting stud
                      {activeInfoModalItem.supporting_study_count === 1 ? 'y' : 'ies'} ·{' '}
                      {activeInfoModalItem.supporting_resource_count} supporting resource
                      {activeInfoModalItem.supporting_resource_count === 1 ? '' : 's'}
                    </Text>
                  </View>

                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Sources</Text>
                    {activeInfoModalItem.sources_summary.length > 0 ? (
                      <View style={styles.sourcesRow}>
                        {activeInfoModalItem.sources_summary.map((source, index) => (
                          <View key={index} style={styles.sourceBadge}>
                            <Text style={styles.sourceBadgeText}>{source}</Text>
                          </View>
                        ))}
                      </View>
                    ) : (
                      <Text style={styles.modalSectionValue}>No specific sources listed.</Text>
                    )}
                  </View>

                  <View style={[styles.modalSection, styles.modalSectionLast]}>
                    <Text style={styles.modalSectionLabel}>Grade Justification</Text>
                    <Text style={[styles.modalSectionValue, styles.modalSummaryText]}>
                      {activeInfoModalItem.grade_justification || 'No justification provided.'}
                    </Text>
                  </View>
                </ScrollView>
              </>
            )}
          </Pressable>
        </Pressable>
      </Modal>

      {/* Rubric & Score Breakdown Modal — total score/grade plus the
          four Multi-Source Confidence Rubric category scores
          (docs/multi_source_confidence_rubric.json). A different rubric
          shape than RecommendedUsesList.tsx's conclusion rubric modal
          (Evidence Strength/Cross-Paper Consensus/Claim Specificity) —
          see MultiSourceRecommendedUse's doc-comment for why these are
          two distinct rubrics entirely. */}
      <Modal
        visible={activeRubricModalItem !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setActiveRubricModalItem(null)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setActiveRubricModalItem(null)}>
          <Pressable style={styles.modalCard} onPress={(event) => event.stopPropagation()}>
            {activeRubricModalItem && isPaperGrade(activeRubricModalItem.confidence_grade) && (
              <>
                <View style={styles.modalHeaderRow}>
                  <Text style={styles.modalTitle} numberOfLines={3}>
                    {activeRubricModalItem.claim}
                  </Text>
                  <Pressable
                    onPress={() => setActiveRubricModalItem(null)}
                    accessibilityRole="button"
                    accessibilityLabel="Close"
                    hitSlop={8}
                  >
                    <Ionicons name="close" size={22} color={colors.orange} />
                  </Pressable>
                </View>

                <View style={styles.modalScoreRow}>
                  <GradeCircleBadge grade={activeRubricModalItem.confidence_grade} large />
                  <Text style={styles.modalScoreText}>
                    {activeRubricModalItem.total_score} / 100 confidence
                  </Text>
                </View>

                <ScrollView style={styles.modalScroll}>
                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>
                      Peer-Reviewed Study Evidence Strength (
                      {activeRubricModalItem.score_breakdown.paper_evidence_quality}/30 pts)
                    </Text>
                  </View>
                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>
                      Official Regulatory & Health Authority Backing (
                      {activeRubricModalItem.score_breakdown.official_authority_backing}/25 pts)
                    </Text>
                  </View>
                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>
                      Multi-Source & Cross-Paper Consensus (
                      {activeRubricModalItem.score_breakdown.multi_source_consensus}/25 pts)
                    </Text>
                  </View>
                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>
                      Claim Specificity & Clinical Actionability (
                      {activeRubricModalItem.score_breakdown.claim_specificity}/20 pts)
                    </Text>
                  </View>
                  <View style={[styles.modalSection, styles.modalSectionLast]}>
                    <Text style={styles.modalSectionLabel}>Grade Justification</Text>
                    <Text style={[styles.modalSectionValue, styles.modalSummaryText]}>
                      {activeRubricModalItem.grade_justification || 'No justification provided.'}
                    </Text>
                  </View>
                </ScrollView>
              </>
            )}
          </Pressable>
        </Pressable>
      </Modal>
    </CollapsibleSection>
  );
};

const styles = StyleSheet.create({
  statusText: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.orange}AA`,
    textAlign: 'center',
    paddingVertical: spacing.sm,
  },
  list: {
    gap: 0,
  },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: spacing.sm,
    paddingVertical: spacing.sm,
    borderStyle: 'dashed',
    borderBottomWidth: 1,
    borderColor: colors.orange,
  },
  rowLast: {
    borderBottomWidth: 0,
  },
  claimText: {
    flex: 1,
    fontSize: typography.resultCardLabel,
    color: colors.orange,
  },
  rowActions: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
  },
  iconButton: {
    padding: spacing.xs,
  },
  // --- Sources badges (info modal) ---
  sourcesRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 6,
  },
  sourceBadge: {
    borderWidth: 1,
    borderColor: colors.orange,
    borderRadius: 10,
    paddingHorizontal: spacing.xs,
    paddingVertical: 2,
    backgroundColor: `${colors.orange}18`,
  },
  sourceBadgeText: {
    fontSize: 11,
    fontWeight: '700',
    color: colors.orange,
  },
  // --- Modals (shared backdrop/card look with the other three lists) ---
  modalBackdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.4)',
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.lg,
  },
  modalCard: {
    width: '100%',
    maxWidth: 480,
    maxHeight: '80%',
    backgroundColor: colors.offWhite,
    borderRadius: 12,
    borderWidth: 2,
    borderColor: colors.orange,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  modalHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    gap: spacing.sm,
  },
  modalTitle: {
    flex: 1,
    fontSize: typography.resultCardTitle,
    fontWeight: '700',
    color: colors.orange,
  },
  modalScoreRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
  },
  modalScoreText: {
    fontSize: typography.resultCardTag,
    fontWeight: '700',
    color: colors.orange,
  },
  modalScroll: {
    maxHeight: 320,
  },
  modalSection: {
    gap: spacing.xs,
    paddingBottom: spacing.sm,
    borderStyle: 'dashed',
    borderBottomWidth: 1,
    borderColor: colors.orange,
  },
  modalSectionLast: {
    borderBottomWidth: 0,
    paddingBottom: 0,
  },
  modalSectionLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  modalSectionValue: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
    lineHeight: 19,
  },
  modalSummaryText: {
    fontStyle: 'italic',
  },
});

export default MultiSourceUsesList;
