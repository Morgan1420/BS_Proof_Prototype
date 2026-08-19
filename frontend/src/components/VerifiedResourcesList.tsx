import React, { useEffect, useMemo, useState } from 'react';
import { View, Text, Pressable, StyleSheet, Modal, ScrollView } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import type { VerifiedResource } from '../services/api';
import { isPaperGrade, sortByGradeThenScore } from '../utils/grades';
import Pagination from './Pagination';
import CollapsibleSection from './CollapsibleSection';
import GradeCircleBadge from './GradeCircleBadge';
import ExternalLinkIconButton from './ExternalLinkIconButton';

/** Max resources shown per page — "Maximum 5 items per page across all
 * lists" (Scientific Information redesign spec). This list had no
 * pagination at all before that unification — every resource rendered
 * on one unbroken page. */
const PAGE_SIZE = 5;

/**
 * "Verified Online Resources" — the third of the three unified,
 * collapsible list panels inside IngredientCard's "Scientific
 * Information" section (see CollapsibleSection.tsx for the shared
 * border/toggle chrome). Renders every VerifiedResource the backend
 * found for this ingredient (app/services/resource_fetcher.py) —
 * official government/regulatory reference links only, since every row
 * already cleared a strict domain allow-list server-side before ever
 * being persisted (see that module's docstring) — there is nothing left
 * for the frontend to filter or validate here, only display.
 *
 * Each row also shows a Phase 8 grade/score badge
 * (backend/app/services/resource_grader.py) whenever the resource has
 * been successfully graded (`resource.grade` non-null). A resource that
 * failed grading (Gemini error at fetch time — best-effort, never
 * retried) simply renders without one, same "null grade = no badge, not
 * an error" convention as every other graded entity in this app.
 *
 * Palette note: same as RecommendedUsesList.tsx/StudiesList.tsx — this
 * only ever renders while its parent IngredientCard is already expanded
 * (all-orange internals), so colors are hardcoded to `colors.orange`
 * rather than conditioned on an `isExpanded` prop.
 */
export interface VerifiedResourcesListProps {
  /** Every stored VerifiedResource for this ingredient — `undefined`
   * means "not fetched yet" (renders the loading state); an empty array
   * means "fetched, but no official reference pages were found" — both
   * render distinct messages (loading vs. the spec's exact empty-state
   * copy below). */
  resources: VerifiedResource[] | undefined;
  isLoading?: boolean;
  errorMessage?: string | null;
}

/**
 * Derives a short authority badge ("NIH" / "USDA" / "EFSA" / "GOV") from
 * a resource's already-domain-allow-listed `domain` field — purely a
 * display convenience, not a re-validation of the domain (the backend has
 * already guaranteed `domain` clears resource_fetcher.py's allow-list by
 * the time this component ever sees it). Also doubles as the General
 * Info Modal's "domain authority rating" field — a genuine, derived
 * categorical rating rather than a fabricated numeric score.
 */
function deriveAuthorityBadge(domain: string): string {
  const normalized = domain.toLowerCase();
  if (normalized.endsWith('nih.gov') || normalized.endsWith('medlineplus.gov')) {
    return 'NIH';
  }
  if (normalized.endsWith('usda.gov')) {
    return 'USDA';
  }
  if (normalized.endsWith('efsa.europa.eu') || normalized.endsWith('.europa.eu')) {
    return 'EFSA';
  }
  if (normalized.endsWith('.gov')) {
    return 'GOV';
  }
  return 'OFFICIAL';
}

const VerifiedResourcesList: React.FC<VerifiedResourcesListProps> = ({
  resources,
  isLoading = false,
  errorMessage = null,
}) => {
  const [page, setPage] = useState(0);
  // General Info Modal — Publisher, domain authority rating, citation
  // count, and summary (per spec; see the modal body below for how the
  // two fields with no backing data on VerifiedResource are handled).
  const [activeInfoModalItem, setActiveInfoModalItem] = useState<VerifiedResource | null>(null);
  // Rubric & Comments Modal — total score/grade plus the AI reviewer's
  // reasoning summary. VerifiedResource deliberately has no per-category
  // breakdown column (see resource_grader.py's own design docstring —
  // just one summary column, not a full rubric JSON), so unlike
  // StudiesList/RecommendedUsesList this modal has no category section.
  const [activeRubricModalItem, setActiveRubricModalItem] = useState<VerifiedResource | null>(
    null
  );

  // Sorted (grade rank, then score descending — see utils/grades.ts::
  // sortByGradeThenScore) once per `resources` change, *before*
  // pagination chunking below, per the Scientific Information section's
  // "sort before paginating" requirement (same rule StudiesList/
  // RecommendedUsesList apply). Unlike those two, this list has no
  // grade-threshold filter — every stored VerifiedResource already
  // cleared the domain allow-list server-side (see this file's own
  // docstring), so nothing is excluded here, only reordered; an ungraded
  // resource (UNGRADED_RANK) simply sorts after every graded one.
  const sortedResources = useMemo<VerifiedResource[] | undefined>(() => {
    return resources
      ? sortByGradeThenScore(resources, (resource) => resource.grade, (resource) => resource.score)
      : undefined;
  }, [resources]);

  const totalPages = sortedResources
    ? Math.max(1, Math.ceil(sortedResources.length / PAGE_SIZE))
    : 1;

  const pageItems = useMemo<VerifiedResource[]>(() => {
    if (!sortedResources) {
      return [];
    }
    const start = page * PAGE_SIZE;
    return sortedResources.slice(start, start + PAGE_SIZE);
  }, [sortedResources, page]);

  // Clamp the current page if the resource list shrinks out from under
  // us (e.g. a fresh grade request replaces `resources` with a
  // different count) — same guard the other two lists apply.
  useEffect(() => {
    setPage((current) => Math.min(current, totalPages - 1));
  }, [totalPages]);

  const totalCount = sortedResources?.length ?? 0;

  return (
    <CollapsibleSection
      title={`Verified Online Resources (Total: ${totalCount})`}
      subheading="Authoritative reference sheets and official health agency documentation."
    >
      {isLoading && !resources ? (
        <Text style={styles.statusText}>Loading verified resources...</Text>
      ) : errorMessage ? (
        <Text style={styles.statusText}>{errorMessage}</Text>
      ) : !resources || resources.length === 0 ? (
        <Text style={styles.statusText}>
          No verified government or regulatory reference pages found for this ingredient.
        </Text>
      ) : (
        <>
          <View style={styles.list}>
            {pageItems.map((resource, index) => (
              <View
                key={resource.id}
                style={[styles.row, index === pageItems.length - 1 && styles.rowLast]}
              >
                <View style={styles.rowLeft}>
                  <Text style={styles.resourceTitle} numberOfLines={2}>
                    {resource.title}
                  </Text>
                  <View style={styles.authorityBadge}>
                    <Text style={styles.authorityBadgeText}>
                      {deriveAuthorityBadge(resource.domain)}
                    </Text>
                  </View>
                </View>

                <View style={styles.rowActions}>
                  {isPaperGrade(resource.grade) && (
                    <GradeCircleBadge
                      grade={resource.grade}
                      onPress={() => setActiveRubricModalItem(resource)}
                    />
                  )}
                  <Pressable
                    style={styles.iconButton}
                    onPress={() => setActiveInfoModalItem(resource)}
                    accessibilityRole="button"
                    accessibilityLabel={`View details for ${resource.title}`}
                    hitSlop={6}
                  >
                    <Ionicons
                      name="information-circle-outline"
                      size={20}
                      color={colors.orange}
                    />
                  </Pressable>
                  <ExternalLinkIconButton
                    url={resource.url}
                    accessibilityLabel={`Open official source for ${resource.title}`}
                  />
                </View>
              </View>
            ))}
          </View>

          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      )}

      {/* General Info Modal — "Publisher, domain authority rating,
          citation count, and summary" per spec. VerifiedResource has no
          citation-count concept at all (that signal applies to papers,
          not official reference pages) — shown as an honest "not
          tracked" label rather than a fabricated number. */}
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

                <ScrollView style={styles.modalScroll}>
                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Publisher</Text>
                    <Text style={styles.modalSectionValue}>{activeInfoModalItem.publisher}</Text>
                  </View>

                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Domain Authority</Text>
                    <Text style={styles.modalSectionValue}>
                      {deriveAuthorityBadge(activeInfoModalItem.domain)} ·{' '}
                      {activeInfoModalItem.domain}
                    </Text>
                  </View>

                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Citation Count</Text>
                    <Text style={styles.modalSectionValue}>
                      Not tracked for this resource type.
                    </Text>
                  </View>

                  <View style={styles.modalSection}>
                    <Text style={styles.modalSectionLabel}>Summary</Text>
                    <Text style={styles.modalSectionValue}>
                      {activeInfoModalItem.summary ?? 'No summary available.'}
                    </Text>
                  </View>

                  {/* Phase 19 — 2-4 short, factual conclusions extracted
                      using this provider's own extraction_instructions
                      (docs/verified_resource_apis.json — see
                      VerifiedResource.extracted_conclusions's docstring
                      in backend/app/models/research.py). Null/empty
                      renders an honest fallback rather than hiding the
                      section. */}
                  <View style={[styles.modalSection, styles.modalSectionLast]}>
                    <Text style={styles.modalSectionLabel}>Extracted Conclusions</Text>
                    {activeInfoModalItem.extracted_conclusions &&
                    activeInfoModalItem.extracted_conclusions.length > 0 ? (
                      activeInfoModalItem.extracted_conclusions.map((conclusion, index) => (
                        <View key={index} style={styles.extractedConclusionRow}>
                          <Text style={styles.extractedConclusionBullet}>{'•'}</Text>
                          <Text style={styles.modalSectionValue}>{conclusion}</Text>
                        </View>
                      ))
                    ) : (
                      <Text style={styles.modalSectionValue}>
                        No specific conclusions extracted for this source yet.
                      </Text>
                    )}
                  </View>
                </ScrollView>
              </>
            )}
          </Pressable>
        </Pressable>
      </Modal>

      {/* Rubric & Comments Modal — total score/grade plus the AI
          reasoning summary. No category breakdown section: unlike
          ResearchPaper/PaperConclusion, VerifiedResource only ever
          persists one summary column, not per-category scores. */}
      <Modal
        visible={activeRubricModalItem !== null}
        transparent
        animationType="fade"
        onRequestClose={() => setActiveRubricModalItem(null)}
      >
        <Pressable style={styles.modalBackdrop} onPress={() => setActiveRubricModalItem(null)}>
          <Pressable style={styles.modalCard} onPress={(event) => event.stopPropagation()}>
            {activeRubricModalItem && isPaperGrade(activeRubricModalItem.grade) && (
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

                <View style={styles.modalScoreRow}>
                  <GradeCircleBadge grade={activeRubricModalItem.grade} large />
                  <Text style={styles.modalScoreText}>
                    {activeRubricModalItem.score ?? '—'} / 100
                  </Text>
                </View>

                <ScrollView style={styles.modalScroll}>
                  <Text style={styles.modalCategoryNote}>
                    Category breakdown not available for this source — official reference pages
                    are graded with a single overall score rather than per-category scoring.
                  </Text>
                  <View style={[styles.modalSection, styles.modalSectionLast]}>
                    <Text style={styles.modalSectionLabel}>AI Reviewer Notes</Text>
                    <Text style={[styles.modalSectionValue, styles.modalSummaryText]}>
                      {activeRubricModalItem.reasoning_summary ?? 'No reviewer notes available.'}
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
  rowLeft: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    minWidth: 0,
  },
  rowActions: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
  },
  iconButton: {
    padding: spacing.xs,
  },
  resourceTitle: {
    flexShrink: 1,
    fontSize: typography.resultCardLabel,
    fontWeight: '600',
    color: colors.orange,
  },
  authorityBadge: {
    borderWidth: 1.5,
    borderColor: colors.orange,
    borderRadius: 999,
    paddingHorizontal: spacing.sm,
    paddingVertical: 2,
  },
  authorityBadgeText: {
    fontSize: 11,
    fontWeight: '700',
    color: colors.orange,
    letterSpacing: 0.5,
  },
  // --- Modals (shared backdrop/card look with StudiesList/RecommendedUsesList) ---
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
  modalCategoryNote: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.orange}AA`,
    lineHeight: 18,
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
  // --- "Extracted Conclusions" bullets (info modal, Phase 19) ---
  extractedConclusionRow: {
    flexDirection: 'row',
    gap: 6,
  },
  extractedConclusionBullet: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
  },
  modalSummaryText: {
    fontStyle: 'italic',
  },
});

export default VerifiedResourcesList;
