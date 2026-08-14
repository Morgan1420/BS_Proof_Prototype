import React, { useEffect, useMemo, useState } from 'react';
import { View, Text, Pressable, StyleSheet, Modal, ScrollView } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import type { PaperConclusion } from '../services/api';
import { GRADE_COLORS, GRADE_RANK, getGradeRank, isPaperGrade } from '../utils/grades';
import Pagination from './Pagination';

/** Max recommendations shown per page — per spec. */
const PAGE_SIZE = 3;

/**
 * "Recomended uses list" — the second block of the redesigned Scientific
 * Information section (see IngredientCard.tsx). Renders every
 * synthesized PaperConclusion (Phase 5 — app/models/research.py on the
 * backend) whose confidence grade clears "C or higher", paginated 3 per
 * page, using the exact same pagination look/interaction as
 * StudiesList's "List of Studies" panel (both share components/
 * Pagination.tsx).
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
  const [activeConclusion, setActiveConclusion] = useState<PaperConclusion | null>(null);

  // "Graded C or higher" — i.e. confidence_grade is A, B, or C (rank 1-3
  // via the shared getGradeRank/GRADE_RANK helpers, same threshold the
  // conclusion_grading_rubric.json's grade_bands use for its own "C"
  // band: min_score 50, same cutoff paper_grading_rubric.json uses).
  // Conclusions with no recognized confidence_grade (rank UNGRADED_RANK)
  // are excluded, same as a D/E grade would be.
  const filteredConclusions = useMemo<PaperConclusion[] | undefined>(() => {
    if (!conclusions) {
      return undefined;
    }
    return conclusions.filter(
      (conclusion) => getGradeRank(conclusion.confidence_grade) <= GRADE_RANK.C
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

  return (
    <View style={styles.container}>
      <Text style={styles.header}>Recomended uses list</Text>

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
                    <View
                      style={[
                        styles.gradeBadge,
                        { backgroundColor: GRADE_COLORS[conclusion.confidence_grade] },
                      ]}
                    >
                      <Text style={styles.gradeBadgeText}>{conclusion.confidence_grade}</Text>
                    </View>
                  )}
                  <Pressable
                    style={styles.iconButton}
                    onPress={() => setActiveConclusion(conclusion)}
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
                </View>
              </View>
            ))}
          </View>

          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      )}

      <Modal
        visible={activeConclusion !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setActiveConclusion(null)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setActiveConclusion(null)}>
          <Pressable style={styles.modalCard} onPress={(event) => event.stopPropagation()}>
            {activeConclusion && (
              <>
                <View style={styles.modalHeaderRow}>
                  <Text style={styles.modalTitle} numberOfLines={4}>
                    {activeConclusion.claim_summary}
                  </Text>
                  <Pressable
                    onPress={() => setActiveConclusion(null)}
                    accessibilityRole="button"
                    accessibilityLabel="Close"
                    hitSlop={8}
                  >
                    <Ionicons name="close" size={22} color={colors.orange} />
                  </Pressable>
                </View>

                <View style={styles.modalScoreRow}>
                  {isPaperGrade(activeConclusion.confidence_grade) && (
                    <View
                      style={[
                        styles.gradeBadge,
                        styles.gradeBadgeLarge,
                        { backgroundColor: GRADE_COLORS[activeConclusion.confidence_grade] },
                      ]}
                    >
                      <Text style={[styles.gradeBadgeText, styles.gradeBadgeTextLarge]}>
                        {activeConclusion.confidence_grade}
                      </Text>
                    </View>
                  )}
                  <Text style={styles.modalScoreText}>
                    {activeConclusion.confidence_score} / 100 confidence
                  </Text>
                </View>

                <ScrollView style={styles.modalScroll}>
                  <Text style={styles.modalDetailText}>
                    {activeConclusion.detailed_conclusion ?? 'No detailed description available.'}
                  </Text>

                  {activeConclusion.dosage_mentioned && (
                    <View style={styles.modalSection}>
                      <Text style={styles.modalSectionLabel}>Dosage Noted</Text>
                      <Text style={styles.modalSectionValue}>
                        {activeConclusion.dosage_mentioned}
                      </Text>
                    </View>
                  )}

                  {activeConclusion.rubric_evaluation && (
                    <>
                      {typeof activeConclusion.rubric_evaluation.evidence_strength_score ===
                        'number' && (
                        <View style={styles.modalSection}>
                          <Text style={styles.modalSectionLabel}>
                            Evidence Strength (
                            {activeConclusion.rubric_evaluation.evidence_strength_score} pts)
                          </Text>
                          <Text style={styles.modalSectionValue}>
                            {activeConclusion.rubric_evaluation.evidence_strength ?? 'N/A'}
                          </Text>
                        </View>
                      )}
                      {typeof activeConclusion.rubric_evaluation.cross_paper_consensus_score ===
                        'number' && (
                        <View style={styles.modalSection}>
                          <Text style={styles.modalSectionLabel}>
                            Cross-Paper Consensus (
                            {activeConclusion.rubric_evaluation.cross_paper_consensus_score} pts)
                          </Text>
                          <Text style={styles.modalSectionValue}>
                            {activeConclusion.rubric_evaluation.cross_paper_consensus ?? 'N/A'}
                          </Text>
                        </View>
                      )}
                      {typeof activeConclusion.rubric_evaluation.claim_specificity_score ===
                        'number' && (
                        <View style={styles.modalSection}>
                          <Text style={styles.modalSectionLabel}>
                            Claim Specificity (
                            {activeConclusion.rubric_evaluation.claim_specificity_score} pts)
                          </Text>
                          <Text style={styles.modalSectionValue}>
                            {activeConclusion.rubric_evaluation.claim_specificity ?? 'N/A'}
                          </Text>
                        </View>
                      )}
                      {activeConclusion.rubric_evaluation.summary_notes && (
                        <View style={[styles.modalSection, styles.modalSectionLast]}>
                          <Text style={styles.modalSectionLabel}>AI Summary Note</Text>
                          <Text style={[styles.modalSectionValue, styles.modalSummaryText]}>
                            {activeConclusion.rubric_evaluation.summary_notes}
                          </Text>
                        </View>
                      )}
                    </>
                  )}

                  <View style={[styles.modalSection, styles.modalSectionLast]}>
                    <Text style={styles.modalSectionLabel}>Paper Support</Text>
                    <Text style={styles.modalSectionValue}>
                      {activeConclusion.supporting_paper_ids.length} supporting ·{' '}
                      {activeConclusion.contradicting_paper_ids.length} contradicting
                    </Text>
                  </View>
                </ScrollView>
              </>
            )}
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    backgroundColor: `${colors.olive}18`,
    borderRadius: 8,
    padding: spacing.md,
    gap: spacing.sm,
  },
  header: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
    textAlign: 'center',
    letterSpacing: 0.5,
  },
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
  gradeBadge: {
    width: 26,
    height: 26,
    borderRadius: 13,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1.5,
    borderColor: colors.orange,
  },
  gradeBadgeLarge: {
    width: 44,
    height: 44,
    borderRadius: 22,
    borderWidth: 2,
  },
  gradeBadgeText: {
    fontSize: 12,
    fontWeight: '700',
    color: '#FFFFFF',
  },
  gradeBadgeTextLarge: {
    fontSize: 18,
  },
  // --- Info modal ---
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
