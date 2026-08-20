import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  View,
  Text,
  Pressable,
  StyleSheet,
  Linking,
  Modal,
  ScrollView,
  Alert,
  ActivityIndicator,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import { gradePaper } from '../services/api';
import type { ResearchPaper } from '../services/api';
import { isPaperGrade, sortByGradeThenScore } from '../utils/grades';
import Pagination from './Pagination';
import CollapsibleSection from './CollapsibleSection';
import GradeCircleBadge, { GRADE_CIRCLE_SIZE } from './GradeCircleBadge';
import ExternalLinkIconButton from './ExternalLinkIconButton';

/** Fill color for the "(-)" ungraded badge — per spec, a neutral gray
 * distinct from both the palette (no gray in theme.ts) and every
 * GRADE_COLORS entry, so an ungraded paper reads as "no signal yet"
 * rather than implying any particular quality. */
const UNGRADED_BADGE_COLOR = '#6C757D';

/** Max rows shown per page — "Maximum 5 items per page across all
 * lists" (Scientific Information redesign spec). */
const PAGE_SIZE = 5;

export interface StudiesListProps {
  /** Every stored ResearchPaper for this ingredient (unpaginated) — see
   * app/schemas/research.py::ResearchPaperResponse on the backend. `undefined`
   * means "not fetched yet" (renders the loading state); an empty array
   * means "fetched, but the ingredient has no stored papers yet" (renders
   * the empty state). */
  papers: ResearchPaper[] | undefined;
  /** True while the initial GET /api/v1/ingredients/{id} fetch is in
   * flight. Only meaningful when `papers` is still undefined. */
  isLoading?: boolean;
  /** Set if the papers fetch failed — shown instead of the list/empty
   * state when non-null. */
  errorMessage?: string | null;
  /** Called with the freshly-graded paper after a successful on-demand
   * single-paper grade (tapping a gray "(-)" badge — see
   * handleGradePaperPress below). The parent (IngredientCard) owns the
   * `papers` array as state and is expected to splice this updated
   * paper back in by id; StudiesList itself doesn't own `papers`, only
   * derives a sorted/paginated view of it, so it has nothing to update
   * on its own. Omitted (rather than required) so any future caller
   * that doesn't need on-demand grading isn't forced to wire it up —
   * without it, the "(-)" badge still triggers the grade request, it
   * just has nowhere to deliver the result. */
  onPaperGraded?: (paper: ResearchPaper) => void;
}

/** Formats the modal's authors/date/domain metadata line, skipping any
 * pieces that are missing rather than showing "undefined" or empty
 * segments. */
function formatMetaLine(
  authors: string | null | undefined,
  publicationDate: string | null | undefined,
  sourceDomain: string
): string {
  const parts = [authors, publicationDate, sourceDomain].filter(
    (part): part is string => Boolean(part && part.trim())
  );
  return parts.join(' · ');
}

/**
 * Paginated "List of Studies" panel — one of the three unified,
 * collapsible list panels inside IngredientCard's "Scientific
 * Information" section (see CollapsibleSection.tsx for the shared
 * border/toggle chrome). Of the three, this one already matched the
 * unified spec almost exactly before the redesign (PAGE_SIZE 5, the
 * two-modal grade-badge-vs-info-icon split) — the redesign only added
 * the collapsible wrapper, the "(Total: N)" title suffix, and renamed
 * this component's modal-selection state to the spec's
 * `activeRubricModalItem`/`activeInfoModalItem` naming.
 *
 * Pagination is entirely local/client-side (all papers for the
 * ingredient are fetched once by the parent and handed to this
 * component) since a single ingredient's paper count is small (a
 * handful to a few dozen from the Phase 2 paper-search pipeline) — no
 * need for server-side paging.
 *
 * Palette note: this component only ever renders while its parent
 * IngredientCard is expanded (see IngredientCard.tsx — it's nested
 * inside `{isExpanded && variant === 'standalone' && (...)}`), so every
 * text/icon/border color here is hardcoded to the palette orange
 * (`colors.orange`) rather than conditioned on an `isExpanded` prop —
 * there is no "collapsed" rendering of this component to also support.
 * This matches the "expanded card = all-orange internals, no green"
 * palette rule (see docs/Architecture.md).
 */
const StudiesList: React.FC<StudiesListProps> = ({
  papers,
  isLoading = false,
  errorMessage = null,
  onPaperGraded,
}) => {
  const [page, setPage] = useState(0);
  // General Info Modal (triggered by the "(i)" icon) — Title, authors,
  // publication date, journal (approximated via source_domain — this
  // app doesn't persist a separate journal-name column, see below),
  // full abstract, and Matched Keywords.
  const [activeInfoModalItem, setActiveInfoModalItem] = useState<ResearchPaper | null>(null);
  // Rubric & Comments Modal (triggered by tapping the round grade badge)
  // — total score/grade, the paper rubric's four categories, and the AI
  // reviewer's summary note.
  const [activeRubricModalItem, setActiveRubricModalItem] = useState<ResearchPaper | null>(null);
  // Id of the paper currently being graded on-demand (tapped "(-)"
  // badge), or null if none is in flight. A single id rather than a Set
  // is enough — the badge is disabled the moment it's tapped (see
  // handleGradePaperPress), so there's at most one in-flight request per
  // StudiesList at a time in practice; still keyed by id (not a bare
  // boolean) so only *that* row's badge swaps to a spinner.
  const [gradingPaperId, setGradingPaperId] = useState<number | null>(null);

  // Sorted (grade rank, then score, then original order — see
  // utils/grades.ts::sortByGradeThenScore) once per `papers` change,
  // *before* pagination chunking below, per spec. Every downstream
  // computation (page count, page slicing, empty-state check) reads from
  // this, not the raw `papers` prop, so a re-sort (e.g. after grading one
  // paper on demand) is immediately reflected in which page a given
  // paper lands on.
  const sortedPapers = useMemo<ResearchPaper[] | undefined>(() => {
    return papers
      ? sortByGradeThenScore(papers, (paper) => paper.grade, (paper) => paper.grade_score)
      : undefined;
  }, [papers]);

  const totalPages = sortedPapers
    ? Math.max(1, Math.ceil(sortedPapers.length / PAGE_SIZE))
    : 1;

  // Clamp the current page if the paper list shrinks/changes out from
  // under us (e.g. a fresh grade request replaces the list with a
  // different count) so we never render an out-of-range page.
  useEffect(() => {
    setPage((current) => Math.min(current, totalPages - 1));
  }, [totalPages]);

  const pageItems = useMemo(() => {
    if (!sortedPapers) {
      return [];
    }
    const start = page * PAGE_SIZE;
    return sortedPapers.slice(start, start + PAGE_SIZE);
  }, [sortedPapers, page]);

  const handleOpenSource = (paper: ResearchPaper): void => {
    Linking.openURL(paper.source_url).catch(() => {
      Alert.alert('Could not open link', paper.source_url);
    });
  };

  /** Tapping a gray "(-)" ungraded badge: grades that one paper on
   * demand (POST /api/v1/papers/{id}/grade) and hands the result up to
   * the parent via `onPaperGraded`. The parent updating its `papers`
   * state is what actually moves this row — `sortedPapers` (above) is
   * derived from that prop via `useMemo`, so once the parent re-renders
   * with the updated paper, this component re-sorts and re-paginates
   * automatically; no local re-sort call needed here.
   */
  const handleGradePaperPress = useCallback(
    (paper: ResearchPaper) => {
      if (gradingPaperId !== null) {
        return; // a grade request is already in flight — ignore extra taps
      }
      setGradingPaperId(paper.id);
      gradePaper(paper.id)
        .then((response) => {
          onPaperGraded?.(response.paper);
        })
        .catch((error) => {
          const message =
            error instanceof Error ? error.message : 'Unknown error occurred.';
          Alert.alert('Grading failed', message);
        })
        .finally(() => {
          setGradingPaperId(null);
        });
    },
    [gradingPaperId, onPaperGraded]
  );

  const totalCount = sortedPapers?.length ?? 0;

  return (
    <CollapsibleSection title={`List of Studies (Total: ${totalCount})`}>
      {isLoading && !sortedPapers ? (
        <Text style={styles.statusText}>Loading studies...</Text>
      ) : errorMessage ? (
        <Text style={styles.statusText}>{errorMessage}</Text>
      ) : !sortedPapers || sortedPapers.length === 0 ? (
        <Text style={styles.statusText}>
          No studies available yet. Click &apos;Grade&apos; to fetch research.
        </Text>
      ) : (
        <>
          <View style={styles.list}>
            {pageItems.map((paper, index) => (
              <View
                key={paper.id}
                style={[
                  styles.row,
                  index === pageItems.length - 1 && styles.rowLast,
                ]}
              >
                <Text style={styles.paperTitle} numberOfLines={2}>
                  {paper.title}
                </Text>
                <View style={styles.rowActions}>
                  {isPaperGrade(paper.grade) ? (
                    <GradeCircleBadge
                      grade={paper.grade}
                      onPress={() => setActiveRubricModalItem(paper)}
                    />
                  ) : gradingPaperId === paper.id ? (
                    <View
                      style={[styles.gradeBadge, styles.gradeBadgeLoading]}
                      accessibilityLabel={`Grading ${paper.title}`}
                      accessibilityState={{ busy: true }}
                    >
                      <ActivityIndicator size="small" color={colors.orange} />
                    </View>
                  ) : (
                    <Pressable
                      onPress={() => handleGradePaperPress(paper)}
                      accessibilityRole="button"
                      accessibilityLabel={`Grade paper: ${paper.title}`}
                      hitSlop={6}
                      style={[styles.gradeBadge, styles.gradeBadgeUngraded]}
                    >
                      <Text style={styles.gradeBadgeText}>-</Text>
                    </Pressable>
                  )}
                  <Pressable
                    style={styles.iconButton}
                    onPress={() => setActiveInfoModalItem(paper)}
                    accessibilityRole="button"
                    accessibilityLabel={`View details for ${paper.title}`}
                    hitSlop={6}
                  >
                    <Ionicons
                      name="information-circle-outline"
                      size={20}
                      color={colors.orange}
                    />
                  </Pressable>
                  <ExternalLinkIconButton
                    url={paper.source_url}
                    accessibilityLabel={`Open original source for ${paper.title}`}
                  />
                </View>
              </View>
            ))}
          </View>

          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      )}

      <Modal
        visible={activeInfoModalItem !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setActiveInfoModalItem(null)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setActiveInfoModalItem(null)}>
          {/* Swallow taps inside the card so they don't bubble to the
              backdrop Pressable and close the modal while reading it. */}
          <Pressable style={styles.modalCard} onPress={(event) => event.stopPropagation()}>
            {activeInfoModalItem && (
              <>
                <View style={styles.modalHeaderRow}>
                  <Text style={styles.modalTitle} numberOfLines={4}>
                    {activeInfoModalItem.title}
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

                {/* "Authors, publication date, journal" — this app has no
                    dedicated `journal` column on ResearchPaper (never
                    persisted at ingestion time), so `source_domain` is
                    shown as the closest available stand-in rather than
                    fabricating a journal name. */}
                <Text style={styles.modalMeta}>
                  {formatMetaLine(
                    activeInfoModalItem.authors,
                    activeInfoModalItem.publication_date,
                    activeInfoModalItem.source_domain
                  ) || 'No metadata available.'}
                </Text>

                {/* "Sample size" — not a raw persisted field; the closest
                    available data is the rubric evaluation's own
                    sample_info free-text description, shown here only
                    when that grading has actually happened. */}
                {activeInfoModalItem.rubric_evaluation?.sample_info && (
                  <View style={styles.metaFieldBlock}>
                    <Text style={styles.metaFieldLabel}>Sample</Text>
                    <Text style={styles.metaFieldValue}>
                      {activeInfoModalItem.rubric_evaluation.sample_info}
                    </Text>
                  </View>
                )}

                {activeInfoModalItem.keywords.length > 0 && (
                  <View style={styles.keywordSection}>
                    <Text style={styles.keywordSectionLabel}>Matched Keywords</Text>
                    <View style={styles.keywordPillRow}>
                      {activeInfoModalItem.keywords.map((keyword) => (
                        <View key={keyword} style={styles.keywordPill}>
                          <Text style={styles.keywordPillText}>{keyword}</Text>
                        </View>
                      ))}
                    </View>
                  </View>
                )}

                <ScrollView style={styles.modalAbstractScroll}>
                  <Text style={styles.modalAbstract}>
                    {activeInfoModalItem.abstract ?? 'No abstract available.'}
                  </Text>
                </ScrollView>

                {/* Phase 19 — 2-4 short, factual findings extracted by
                    the same Gemini call that grades this paper (see
                    ResearchPaper.extracted_conclusions's docstring in
                    backend/app/models/research.py). Null/empty renders an
                    honest fallback rather than hiding the section, so the
                    frontend never silently implies extraction ran when it
                    hasn't. */}
                <View style={styles.extractedConclusionsSection}>
                  <Text style={styles.extractedConclusionsLabel}>Extracted Conclusions</Text>
                  {activeInfoModalItem.extracted_conclusions &&
                  activeInfoModalItem.extracted_conclusions.length > 0 ? (
                    activeInfoModalItem.extracted_conclusions.map((conclusion, index) => (
                      <View key={index} style={styles.extractedConclusionRow}>
                        <Text style={styles.extractedConclusionBullet}>{'•'}</Text>
                        <Text style={styles.extractedConclusionText}>{conclusion}</Text>
                      </View>
                    ))
                  ) : (
                    <Text style={styles.extractedConclusionsEmpty}>
                      No specific conclusions extracted for this source yet.
                    </Text>
                  )}
                </View>

                <Pressable
                  style={styles.modalLinkButton}
                  onPress={() => handleOpenSource(activeInfoModalItem)}
                  accessibilityRole="button"
                  accessibilityLabel="Open original source"
                >
                  <Ionicons name="globe-outline" size={16} color={colors.offWhite} />
                  <Text style={styles.modalLinkButtonText}>View Source</Text>
                </Pressable>
              </>
            )}
          </Pressable>
        </Pressable>
      </Modal>

      <Modal
        visible={activeRubricModalItem !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setActiveRubricModalItem(null)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setActiveRubricModalItem(null)}>
          <Pressable style={styles.modalCard} onPress={(event) => event.stopPropagation()}>
            {activeRubricModalItem &&
              isPaperGrade(activeRubricModalItem.grade) &&
              activeRubricModalItem.rubric_evaluation && (
                <>
                  <View style={styles.modalHeaderRow}>
                    <Text style={styles.modalTitle} numberOfLines={3}>
                      {activeRubricModalItem.title}
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

                  <View style={styles.rubricScoreRow}>
                    <GradeCircleBadge grade={activeRubricModalItem.grade} large />
                    <Text style={styles.rubricTotalScore}>
                      {activeRubricModalItem.rubric_evaluation.total_score} / 100
                    </Text>
                  </View>

                  <ScrollView style={styles.modalAbstractScroll}>
                    {/* Phase 31 — standardized grade-modal formatting:
                        every category header now shows "(earned/max pts)",
                        not just the earned score, matching
                        ScientificConclusionsList.tsx's rubric modal and
                        the task spec's `[Category] ([Score]/[Max] pts)`
                        format. Max values are docs/paper_grading_rubric.json's
                        own `max_score` per category — hardcoded here the
                        same way ScientificConclusionsList.tsx already
                        hardcodes its own rubric's /30, /25, /25, /20,
                        rather than threading the rubric JSON through as a
                        prop for four display-only literals.

                        Phase 32 — rubric v1.6 rebalance: study_type 40 ->
                        45, journal_reputation 15 -> 10 (sample_methodology
                        and funding_bias unchanged at 40/5) — these two
                        literals below were updated to match; see
                        docs/paper_grading_rubric.json and
                        paper_grader.py's own v1.6 docstring notes. */}
                    <View style={styles.rubricSection}>
                      <Text style={styles.rubricSectionLabel}>
                        Study Design ({activeRubricModalItem.rubric_evaluation.study_type_score}/45 pts)
                      </Text>
                      <Text style={styles.rubricSectionValue}>
                        {activeRubricModalItem.rubric_evaluation.study_type}
                      </Text>
                    </View>

                    <View style={styles.rubricSection}>
                      <Text style={styles.rubricSectionLabel}>
                        Journal Rigor ({activeRubricModalItem.rubric_evaluation.journal_score}/10 pts)
                      </Text>
                      <Text style={styles.rubricSectionValue}>
                        {activeRubricModalItem.rubric_evaluation.journal_reputation}
                      </Text>
                    </View>

                    <View style={styles.rubricSection}>
                      <Text style={styles.rubricSectionLabel}>
                        Methodology &amp; Sample (
                        {activeRubricModalItem.rubric_evaluation.sample_score}/40 pts)
                      </Text>
                      <Text style={styles.rubricSectionValue}>
                        {activeRubricModalItem.rubric_evaluation.sample_info}
                      </Text>
                    </View>

                    <View style={styles.rubricSection}>
                      <Text style={styles.rubricSectionLabel}>
                        Funding &amp; Bias ({activeRubricModalItem.rubric_evaluation.funding_score}
                        /5 pts)
                      </Text>
                      <Text style={styles.rubricSectionValue}>
                        {activeRubricModalItem.rubric_evaluation.funding_status}
                      </Text>
                    </View>

                    <View style={[styles.rubricSection, styles.rubricSectionLast]}>
                      <Text style={styles.rubricSectionLabel}>AI Summary Note</Text>
                      <Text style={[styles.rubricSectionValue, styles.rubricSummaryText]}>
                        {activeRubricModalItem.rubric_evaluation.summary_notes}
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
    // Palette orange rather than a neutral gray — this component only
    // ever renders inside an expanded card, so every visible line/border
    // here follows the same "orange, not green" rule as the text/icons.
    borderColor: colors.orange,
  },
  rowLast: {
    borderBottomWidth: 0,
  },
  paperTitle: {
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
  // Ungraded/loading badge — matches GradeCircleBadge's own footprint
  // exactly (same GRADE_CIRCLE_SIZE) so the row doesn't reflow when a
  // paper transitions between the two states.
  gradeBadge: {
    width: GRADE_CIRCLE_SIZE,
    height: GRADE_CIRCLE_SIZE,
    borderRadius: GRADE_CIRCLE_SIZE / 2,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1.5,
    borderColor: colors.orange,
  },
  gradeBadgeText: {
    fontSize: 12,
    fontWeight: '700',
    color: '#FFFFFF',
  },
  // Gray fill for an ungraded paper's tappable "(-)" badge — same
  // circular shape/orange border as a lettered badge (per spec: "Maintain
  // the existing circular badge styling and active orange border rules"),
  // only the fill color and content differ.
  gradeBadgeUngraded: {
    backgroundColor: UNGRADED_BADGE_COLOR,
  },
  // Swapped in for the ungraded badge while a grade request for that
  // paper is in flight — keeps the same circular/bordered footprint so
  // the row doesn't reflow, just replaces the fill+content with a
  // transparent background and a spinner.
  gradeBadgeLoading: {
    backgroundColor: 'transparent',
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
  modalMeta: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.orange}CC`,
  },
  metaFieldBlock: {
    gap: 2,
  },
  metaFieldLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  metaFieldValue: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
    lineHeight: 19,
  },
  // --- "Matched Keywords" pills (info modal) ---
  keywordSection: {
    gap: spacing.xs,
  },
  keywordSectionLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  keywordPillRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.xs,
  },
  keywordPill: {
    borderWidth: 1,
    borderColor: colors.orange,
    borderRadius: 12,
    paddingHorizontal: 10,
    paddingVertical: 3,
    backgroundColor: `${colors.orange}18`,
  },
  keywordPillText: {
    fontSize: typography.resultCardLabel,
    fontWeight: '600',
    color: colors.orange,
  },
  modalAbstractScroll: {
    maxHeight: 220,
  },
  modalAbstract: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
    lineHeight: 19,
  },
  // --- "Extracted Conclusions" (info modal, Phase 19) ---
  extractedConclusionsSection: {
    gap: spacing.xs,
  },
  extractedConclusionsLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  extractedConclusionRow: {
    flexDirection: 'row',
    gap: 6,
  },
  extractedConclusionBullet: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
  },
  extractedConclusionText: {
    flex: 1,
    fontSize: typography.resultCardLabel,
    color: colors.orange,
    lineHeight: 19,
  },
  extractedConclusionsEmpty: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.orange}99`,
  },
  // --- Rubric & Comments modal ---
  rubricScoreRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
  },
  rubricTotalScore: {
    fontSize: typography.resultCardTitle,
    fontWeight: '700',
    color: colors.orange,
  },
  rubricSection: {
    gap: spacing.xs,
    paddingBottom: spacing.sm,
    borderStyle: 'dashed',
    borderBottomWidth: 1,
    borderColor: colors.orange,
  },
  rubricSectionLast: {
    borderBottomWidth: 0,
    paddingBottom: 0,
  },
  rubricSectionLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  rubricSectionValue: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
    lineHeight: 19,
  },
  rubricSummaryText: {
    fontStyle: 'italic',
  },
  modalLinkButton: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.xs,
    backgroundColor: colors.orange,
    borderRadius: 8,
    paddingVertical: spacing.sm,
    marginTop: spacing.xs,
  },
  modalLinkButtonText: {
    fontSize: typography.buttonLabel,
    fontWeight: '700',
    color: colors.offWhite,
  },
});

export default StudiesList;
