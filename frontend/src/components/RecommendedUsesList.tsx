/**
 * @deprecated Phase 29 — removed from IngredientCard.tsx's "Scientific
 * Information" section to eliminate UI duplication/confusion against
 * `ScientificConclusionsList.tsx`, which is now the single source of
 * truth for "what is this ingredient good for" content. This file is
 * left in place (not deleted) per this codebase's "deprecate, don't
 * delete" convention for retired modules (see `MultiSourceUsesList.tsx`,
 * deprecated the same way in Phase 24), and because destructive file
 * operations require explicit user action rather than being run
 * automatically. It is not imported or rendered anywhere in the app —
 * `IngredientCard.tsx` no longer imports `RecommendedUsesList`. Do not
 * import from this file; do not add new features here.
 *
 * Note this is a DIFFERENT component from `MultiSourceUsesList.tsx`:
 * that one rendered the Phase 23/24 Ingredient-level
 * `scientific_conclusions` array under its pre-rename name; this one
 * renders the still-active, unrelated Phase 5 per-paper `PaperConclusion`
 * data (see `IngredientCard.tsx`'s `conclusions` prop doc-comment) — the
 * backend data this component reads (`PaperConclusion` rows via
 * `conclusion_grader.py::process_paper_conclusions`) is NOT deprecated
 * and still runs every grade request: it remains genuine input evidence
 * for `synthesize_ingredient_summary`'s (Stage 2) synthesis of
 * `scientific_conclusions` itself. Only this component's own dedicated
 * UI list panel was removed, not the underlying data or its backend
 * pipeline.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { View, Text, Pressable, StyleSheet, Modal, ScrollView } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import type { PaperConclusion } from '../services/api';
import { GRADE_RANK, getGradeRank, isPaperGrade, sortByGradeThenScore } from '../utils/grades';
import Pagination from './Pagination';
import CollapsibleSection from './CollapsibleSection';
import GradeCircleBadge from './GradeCircleBadge';

/** Max recommendations shown per page — "Maximum 5 items per page
 * across all lists" (Scientific Information redesign spec; was 3 before
 * that unification). */
const PAGE_SIZE = 5;

/**
 * "Recommended Uses List" — the second of the three unified, collapsible
 * list panels inside IngredientCard's "Scientific Information" section
 * (see CollapsibleSection.tsx for the shared border/toggle chrome).
 * Renders every synthesized PaperConclusion (Phase 5 — app/models/
 * research.py on the backend) whose confidence grade clears "C or
 * higher", paginated using the same components/Pagination.tsx as the
 * other two lists.
 *
 * Unlike StudiesList/VerifiedResourcesList, this list's rows never show
 * a website icon — a PaperConclusion is a synthesized cross-paper claim,
 * not a single external page, so there's no one URL for a row to open.
 *
 * Per the redesign spec, tapping a row's info icon and tapping its grade
 * badge now open two *separate* modals instead of one combined one:
 * `activeInfoModalItem` (general metadata: supporting/contradicting
 * study counts, dosage notes, confidence score) and
 * `activeRubricModalItem` (the conclusion rubric's own category
 * breakdown — Evidence Strength, Cross-Paper Consensus, Claim
 * Specificity — plus the AI reviewer's summary note).
 *
 * Palette note: same as StudiesList.tsx — this only ever renders while
 * its parent IngredientCard is already expanded (all-orange internals),
 * so colors are hardcoded to `colors.orange` rather than conditioned on
 * an `isExpanded` prop.
 */
export interface RecommendedUsesListProps {
  /** Every stored PaperConclusion for this ingredient (unfiltered,
   * unpaginated) — see IngredientDetailResponse.conclusions on the
   * backend. `undefined` means "not fetched yet" (renders the loading
   * state); an empty array means "fetched, but nothing meets the
   * quality threshold yet" (or the ingredient has no conclusions at
   * all) — both render the same empty-state message, since neither case
   * is actionable differently from the user's point of view. */
  conclusions: PaperConclusion[] | undefined;
  isLoading?: boolean;
  errorMessage?: string | null;
}

const RecommendedUsesList: React.FC<RecommendedUsesListProps> = ({
  conclusions,
  isLoading = false,
  errorMessage = null,
}) => {
  const [page, setPage] = useState<number>(0);
  const [activeInfoModalItem, setActiveInfoModalItem] = useState<PaperConclusion | null>(null);
  const [activeRubricModalItem, setActiveRubricModalItem] = useState<PaperConclusion | null>(
    null
  );

  // "Graded C or higher" — i.e. confidence_grade is A, B, or C (rank 1-3
  // via the shared getGradeRank/GRADE_RANK helpers, same threshold the
  // conclusion_grading_rubric.json's grade_bands use for its own "C"
  // band: min_score 50, same cutoff paper_grading_rubric.json uses).
  // Conclusions with no recognized confidence_grade (rank UNGRADED_RANK)
  // are excluded, same as a D/E grade would be. Sorted (grade rank, then
  // confidence_score descending — see utils/grades.ts::
  // sortByGradeThenScore) immediately after filtering, *before*
  // pagination chunking below, per the Scientific Information section's
  // "sort before paginating" requirement (same rule StudiesList/
  // VerifiedResourcesList apply to their own lists).
  const filteredConclusions = useMemo<PaperConclusion[] | undefined>(() => {
    if (!conclusions) {
      return undefined;
    }
    const eligible = conclusions.filter(
      (conclusion) => getGradeRank(conclusion.confidence_grade) <= GRADE_RANK.C
    );
    return sortByGradeThenScore(
      eligible,
      (conclusion) => conclusion.confidence_grade,
      (conclusion) => conclusion.confidence_score
    );
  }, [conclusions]);

  const totalPages = filteredConclusions
    ? Math.max(1, Math.ceil(filteredConclusions.length / PAGE_SIZE))
    : 1;

  const pageItems = useMemo<PaperConclusion[]>(() => {
    if (!filteredConclusions) {
      return [];
    }
    const start = page * PAGE_SIZE;
    return filteredConclusions.slice(start, start + PAGE_SIZE);
  }, [filteredConclusions, page]);

  // Clamp the current page if the filtered list shrinks out from under
  // us (e.g. a fresh grade request replaces `conclusions` with fewer
  // qualifying entries) — same guard StudiesList applies to its own
  // pagination.
  useEffect(() => {
    setPage((current) => Math.min(current, totalPages - 1));
  }, [totalPages]);

  const totalCount = filteredConclusions?.length ?? 0;

  return (
    <CollapsibleSection title={`Recommended Uses List (Total: ${totalCount})`}>
      {isLoading && !filteredConclusions ? (
        <Text style={styles.statusText}>Loading recommendations...</Text>
      ) : errorMessage ? (
        <Text style={styles.statusText}>{errorMessage}</Text>
      ) : !filteredConclusions || filteredConclusions.length === 0 ? (
        <Text style={styles.statusText}>
          No recommendations found yet for papers graded C or higher.
        </Text>
      ) : (
        <>
          <View style={styles.list}>
            {pageItems.map((conclusion, index) => (
              <View
                key={conclusion.id}
                style={[styles.row, index === pageItems.length - 1 && styles.rowLast]}
              >
                <Text style={styles.claimText} numberOfLines={2}>
                  {conclusion.claim_summary}
                </Text>
                <View style={styles.rowActions}>
                  {isPaperGrade(conclusion.confidence_grade) && (
                    <GradeCircleBadge
                      grade={conclusion.confidence_grade}
                      onPress={() => setActiveRubricModalItem(conclusion)}
                    />
                  )}
                  <Pressable
                    style={styles.iconButton}
                    onPress={() => setActiveInfoModalItem(conclusion)}
                    accessibilityRole="button"
                    accessibilityLabel={`View details for ${conclusion.claim_summary}`}
                    hitSlop={6}
                  >
                    <Ionicons
                      name="information-circle-outline"
                      size={20}
                      color={colors.orange}
                    />
                  </Pressable>
                  {/* Deliberately no website icon here — a conclusion is a
                      synthesized claim, not a single external page. */}
                </View>
              </View>
            ))}
          </View>

          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      )}

      {/* General Info Modal — "Supporting study count, contradicting
          study count, dosage notes, and confidence score" per spec, plus
          the claim/detailed-conclusion text itself as context. */}
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
                    {activeInfoModalItem.claim_summary}
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
                  <Text style={styles.modalDetailText}>
                    {activeInfoModalItem.detailed_conclusion ??
                      'No detailed description available.'}
                  </Text>

                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Confidence Score</Text>
                    <Text style={styles.modalSectionValue}>
                      {activeInfoModalItem.confidence_score} / 100
                    </Text>
                  </View>

                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Dosage Notes</Text>
                    <Text style={styles.modalSectionValue}>
                      {activeInfoModalItem.dosage_mentioned ?? 'Not mentioned in reviewed studies.'}
                    </Text>
                  </View>

                  <View style={[styles.modalSection, styles.modalSectionLast]}>
                    <Text style={styles.modalSectionLabel}>Paper Support</Text>
                    <Text style={styles.modalSectionValue}>
                      {activeInfoModalItem.supporting_paper_ids.length} supporting ·{' '}
                      {activeInfoModalItem.contradicting_paper_ids.length} contradicting
                    </Text>
                  </View>
                </ScrollView>
              </>
            )}
          </Pressable>
        </Pressable>
      </Modal>

      {/* Rubric & Comments Modal — total score/grade, the conclusion
          rubric's own three categories (this is a different rubric shape
          than a paper's — see ConclusionRubricEvaluation in api.ts — so
          it doesn't reuse StudiesList's Study Design/Journal Rigor/etc.
          labels), and the AI reviewer's summary note. */}
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
                    {activeRubricModalItem.claim_summary}
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
                    {activeRubricModalItem.confidence_score} / 100 confidence
                  </Text>
                </View>

                <ScrollView style={styles.modalScroll}>
                  {activeRubricModalItem.rubric_evaluation ? (
                    <>
                      {typeof activeRubricModalItem.rubric_evaluation
                        .evidence_strength_score === 'number' && (
                        <View style={styles.modalSection}>
                          <Text style={styles.modalSectionLabel}>
                            Evidence Strength (
                            {activeRubricModalItem.rubric_evaluation.evidence_strength_score} pts)
                          </Text>
                          <Text style={styles.modalSectionValue}>
                            {activeRubricModalItem.rubric_evaluation.evidence_strength ?? 'N/A'}
                          </Text>
                        </View>
                      )}
                      {typeof activeRubricModalItem.rubric_evaluation
                        .cross_paper_consensus_score === 'number' && (
                        <View style={styles.modalSection}>
                          <Text style={styles.modalSectionLabel}>
                            Cross-Paper Consensus (
                            {activeRubricModalItem.rubric_evaluation.cross_paper_consensus_score}{' '}
                            pts)
                          </Text>
                          <Text style={styles.modalSectionValue}>
                            {activeRubricModalItem.rubric_evaluation.cross_paper_consensus ??
                              'N/A'}
                          </Text>
                        </View>
                      )}
                      {typeof activeRubricModalItem.rubric_evaluation
                        .claim_specificity_score === 'number' && (
                        <View style={styles.modalSection}>
                          <Text style={styles.modalSectionLabel}>
                            Claim Specificity (
                            {activeRubricModalItem.rubric_evaluation.claim_specificity_score} pts)
                          </Text>
                          <Text style={styles.modalSectionValue}>
                            {activeRubricModalItem.rubric_evaluation.claim_specificity ?? 'N/A'}
                          </Text>
                        </View>
                      )}
                      <View style={[styles.modalSection, styles.modalSectionLast]}>
                        <Text style={styles.modalSectionLabel}>AI Summary Note</Text>
                        <Text style={[styles.modalSectionValue, styles.modalSummaryText]}>
                          {activeRubricModalItem.rubric_evaluation.summary_notes ??
                            'No reviewer notes available.'}
                        </Text>
                      </View>
                    </>
                  ) : (
                    <Text style={styles.modalSectionValue}>
                      No rubric breakdown available for this recommendation.
                    </Text>
                  )}
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
  // --- Modals (shared backdrop/card look with StudiesList/VerifiedResourcesList) ---
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
  modalDetailText: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
    lineHeight: 19,
    marginBottom: spacing.sm,
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

export default RecommendedUsesList;
