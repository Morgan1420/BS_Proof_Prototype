import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { View, Text, Pressable, StyleSheet, Alert, Image } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import { animateCardToggle } from '../utils/animations';
import { computeAverageGrade } from '../utils/grades';
import GradeBadge, { PLACEHOLDER_GRADE_VALUE } from './GradeBadge';
import StudiesList from './StudiesList';
import RecommendedUsesList from './RecommendedUsesList';
import VerifiedResourcesList from './VerifiedResourcesList';
import StandaloneInfoSection from './StandaloneInfoSection';
import Pagination from './Pagination';
import {
  fetchIngredientDetail,
  gradeIngredient,
  PAPER_STATUS_DISCARDED_IRRELEVANT,
} from '../services/api';
import type { PaperConclusion, ResearchPaper, VerifiedResource } from '../services/api';

/** Max related products shown per page — same "5 per list page" rule
 * every other Scientific Information list panel uses (see
 * StudiesList.tsx/RecommendedUsesList.tsx/VerifiedResourcesList.tsx's own
 * PAGE_SIZE constants). */
const RELATED_PRODUCTS_PAGE_SIZE = 5;

/** One product this ingredient appears in, for the "Related Products"
 * section below. No backend endpoint returns this list yet (Ingredient
 * is canonical/shared data — see this file's own docs on the M2M schema
 * — and the only thing GET /api/v1/ingredients/{id} currently exposes
 * about an ingredient's products is the bare `productCount` number, not
 * which products they are). Defined here, and wired all the way through
 * `Ingredient.relatedProducts` below, so the moment a
 * "GET /api/v1/ingredients/{id}/products"-shaped endpoint exists this
 * section only needs a data source plugged in, not a UI rewrite — see
 * the empty-state handling further down for how this gap is surfaced
 * honestly in the meantime rather than fabricating rows. */
export interface RelatedProduct {
  id: number;
  name: string;
  brand?: string;
  thumbnailUrl?: string;
}

/**
 * A single ingredient/nutrient row. This card is used in two different
 * contexts with two different data shapes available:
 *
 * 1. Nested inside a ProductCard: `amount`/`unit`/`dailyValue` are that
 *    product's specific dosage for this ingredient (from
 *    ProductIngredientLink on the backend).
 * 2. Standalone on ResultsScreen (a bare ingredient search result):
 *    there's no single product's dosage to show, since Ingredient is now
 *    canonical/shared data — instead `recommendedDailyDosage`/
 *    `scientificData`/`productCount` (from the canonical Ingredient row)
 *    are populated instead. See ResultsScreen.tsx::toIngredient().
 *
 * All of these are optional and the component renders whichever set is
 * actually present.
 */
export interface Ingredient {
  id: number;
  productId?: number;
  name: string;
  // Product-specific dosage (context 1 above).
  amount?: string;
  unit?: string;
  dailyValue?: string;
  productName?: string;
  // Canonical ingredient metadata (context 2 above).
  recommendedDailyDosage?: string;
  scientificData?: string;
  productCount?: number;
  /** Whether this ingredient already has a grade. Every mapping function
   * that builds an `Ingredient` currently initializes this to `false` —
   * search results don't yet come back with real grading data (see
   * docs/Architecture.md's "Known gaps") — so in practice this only ever
   * becomes `true` locally, after a successful
   * POST /api/v1/ingredients/{id}/grade call (see this component's
   * `handleGradeRequest` below). Only meaningful for the `'standalone'`
   * variant (see `variant` below) — nested ingredient rows don't render
   * a grade badge at all. */
  is_graded?: boolean;
  /** Debug grade text from the grading API (e.g. "14 / 14 / 14",
   * formatted server-side as the ingredient's stored paper count
   * repeated three times — see backend/app/services/grading.py). Only
   * meaningful once `is_graded` is true; like `is_graded`, nothing
   * populates this with a real value before a grade request has been
   * made, so it's effectively always `undefined` on initial load today. */
  grade_badge_text?: string;
  /** Gemini-synthesized 1-2 sentence overview combining BOTH graded
   * ResearchPaper findings and VerifiedResource official guidance
   * (NIH/USDA/EFSA/Health Canada/...) — see backend/app/services/
   * conclusion_grader.py::synthesize_ingredient_summary. Same
   * "undefined = not loaded yet" convention as `papers`/`conclusions`/
   * `verified_resources` below; also legitimately absent (not just
   * unloaded) when the backend had no evidence to synthesize from yet —
   * see `scientificSummary`'s fallback logic below for how that's
   * handled. */
  summary_description?: string;
  /** Every stored ResearchPaper for this ingredient (see
   * backend/app/models/research.py), rendered by StudiesList inside the
   * standalone variant's "Scientific information" section. `undefined`
   * means "not loaded yet" — IngredientCard fetches it lazily on first
   * expand (via GET /api/v1/ingredients/{id}) rather than requiring
   * every caller to populate it upfront, since most ingredient lists
   * (search results, product-nested rows) don't need it until a card is
   * actually opened. Only meaningful for the `'standalone'` variant. */
  papers?: ResearchPaper[];
  /** Every synthesized PaperConclusion for this ingredient (Phase 5 —
   * see backend/app/models/research.py), rendered by RecommendedUsesList
   * in the standalone variant's "Scientific information" block. Same
   * "undefined = not loaded yet, lazily fetched on first expand"
   * convention as `papers` above. */
  conclusions?: PaperConclusion[];
  /** Every stored VerifiedResource for this ingredient (Phase 7 — see
   * backend/app/models/research.py), rendered by VerifiedResourcesList in
   * the standalone variant's "Scientific information" block. Same
   * "undefined = not loaded yet, lazily fetched on first expand"
   * convention as `papers`/`conclusions` above. */
  verified_resources?: VerifiedResource[];
  /** Phase 19 note: this component does NOT own any "(i)" info-modal
   * markup itself — it is a pure passthrough that hands `papers`/
   * `verified_resources` straight to StudiesList/VerifiedResourcesList
   * below (see the JSX further down this file), and each of those two
   * components independently implements its own `activeInfoModalItem`
   * Modal. The `extracted_conclusions` field added to `ResearchPaper`/
   * `VerifiedResource` (src/services/api.ts, Phase 19) therefore flows
   * through to those modals automatically once the item objects carry
   * it — no transformation logic was needed (or added) in this file. See
   * StudiesList.tsx's and VerifiedResourcesList.tsx's own "Extracted
   * Conclusions" modal sections for the actual rendering. */
  /** Every product this ingredient appears in, for the "Related
   * Products" section. `undefined` (the only value any current caller
   * ever sets — see RelatedProduct's own docstring) means "no backend
   * data source for this yet"; a real caller would use an empty array to
   * mean "confirmed zero products" instead. Only meaningful for the
   * `'standalone'` variant. */
  relatedProducts?: RelatedProduct[];
}

export type IngredientCardVariant = 'nested' | 'standalone';

export interface IngredientCardProps {
  ingredient: Ingredient;
  /** Whether this card is currently expanded. Controlled by the parent
   * (ProductCard for nested ingredients, or a screen for standalone
   * results) so only one card in a group can be open at once. */
  isExpanded: boolean;
  /** Called when the header is tapped; the parent decides what "open"
   * means (usually: toggle this id, closing any other open sibling). */
  onToggle: () => void;
  /** Which internal layout to render. `'nested'` (the default) is the
   * original compact dosage/product-relation card used inside
   * ProductCard's own ingredient accordion — unchanged. `'standalone'` is
   * the wireframe-driven layout (name + grade badge header, three stacked
   * placeholder info blocks plus the "Scientific information" composite
   * section) used for top-level ingredient results on ResultsScreen,
   * where an ingredient isn't tied to one particular product's dosage.
   * Defaulting to `'nested'` means ProductCard's existing usage doesn't
   * need to change at all. */
  variant?: IngredientCardVariant;
}

/** Placeholder body copy for the two still-placeholder standalone
 * sections ("General Information"/"Grade Info" — real content tracked in
 * docs/Architecture.md's "Expandable cards" follow-up). Each is now
 * rendered as its own `StandaloneInfoSection` (see the render logic
 * below) rather than a shared generic array/map — the section *title* is
 * the `StandaloneInfoSection` prop, not part of this text, now that each
 * one is independently collapsible with its own header. */
const GENERAL_INFORMATION_PLACEHOLDER =
  'General ingredient summary and usage information placeholder...';
const GRADE_INFO_PLACEHOLDER =
  'Detailed breakdown of safety, efficacy, and purity grade criteria placeholder...';

/** Accordion card for a single ingredient. Expansion state is entirely
 * controlled by the parent — see IngredientCardProps.isExpanded/onToggle
 * — so a group of these can implement single-expansion (only one open at
 * a time) by sharing one `expandedId` state value.
 *
 * Forwards `ref` to its outer `View` so ProductCard can attach a ref
 * directly to a nested ingredient row (no extra wrapping View needed) —
 * used to auto-scroll a just-expanded ingredient into view on web via
 * `ref.current.scrollIntoView(...)` (React Native Web forwards `View`
 * refs to the underlying DOM node, which supports it natively).
 */
const IngredientCard = React.forwardRef<View, IngredientCardProps>(
  function IngredientCard({ ingredient, isExpanded, onToggle, variant = 'nested' }, ref) {
    const doseSummary =
      ingredient.amount && ingredient.unit
        ? `${ingredient.amount}${ingredient.unit}`
        : ingredient.recommendedDailyDosage
        ? `RDA: ${ingredient.recommendedDailyDosage}`
        : 'dosage unavailable';

    // "Graded" state — only meaningful (and only rendered) for the
    // standalone variant; harmless to keep these hooks unconditional for
    // the nested variant too (Rules of Hooks), they're simply unused
    // there. Unlike ProductCard's still-local-only placeholder grading,
    // this is backed by a real POST /api/v1/ingredients/{id}/grade call
    // (see handleGradeRequest below) — isGraded/gradeBadgeText reflect
    // whatever that call actually returned, not an instant local flip.
    const [isGraded, setIsGraded] = useState(ingredient.is_graded ?? false);
    const [gradeBadgeText, setGradeBadgeText] = useState(
      ingredient.grade_badge_text ?? PLACEHOLDER_GRADE_VALUE
    );
    // Drives GradeBadge's loading spinner while the request is in
    // flight — the backend call chains Gemini keyword generation plus
    // several sequential external paper-search API calls, so this can
    // take a few seconds, not feel instant like ProductCard's local flip.
    const [isRequestingGrade, setIsRequestingGrade] = useState(false);

    // --- StudiesList data (standalone variant's "Scientific information" section) ---
    // `undefined` = not fetched yet, `[]` = fetched and genuinely empty.
    // Seeded from `ingredient.papers` when the caller already has it (e.g.
    // a parent that just re-rendered this card with fresh data), so we
    // don't refetch something we were already handed.
    const [papers, setPapers] = useState<ResearchPaper[] | undefined>(
      ingredient.papers
    );
    const [papersLoading, setPapersLoading] = useState(false);
    const [papersError, setPapersError] = useState<string | null>(null);
    // Guards the lazy fetch below to "at most once per mount" rather than
    // retrying on every render while `papers` stays undefined (e.g. after
    // a fetch error) — a manual re-expand (collapse/expand again) doesn't
    // remount this component, so this ref persists across that; a fresh
    // grade request (handleGradeRequest below) updates `papers` directly
    // instead of going through this fetch path at all.
    const papersFetchAttemptedRef = useRef(ingredient.papers !== undefined);

    // --- RecommendedUsesList data (standalone variant's "Scientific
    // information" block, Phase 5) — same undefined/loading/error/fetch-
    // once-per-mount conventions as `papers` above. Populated by the same
    // GET /api/v1/ingredients/{id} call as `papers` (one fetch, both
    // fields on the response) — see the shared effect below.
    const [conclusions, setConclusions] = useState<PaperConclusion[] | undefined>(
      ingredient.conclusions
    );
    const [conclusionsLoading, setConclusionsLoading] = useState(false);
    const [conclusionsError, setConclusionsError] = useState<string | null>(null);

    // --- VerifiedResourcesList data (standalone variant's "Scientific
    // information" block, Phase 7) — same undefined/loading/error/fetch-
    // once-per-mount conventions as `papers`/`conclusions` above.
    // Populated by the same GET /api/v1/ingredients/{id} call as both
    // (one fetch, all three fields on the response) — see the shared
    // effect below.
    const [verifiedResources, setVerifiedResources] = useState<VerifiedResource[] | undefined>(
      ingredient.verified_resources
    );
    const [verifiedResourcesLoading, setVerifiedResourcesLoading] = useState(false);
    const [verifiedResourcesError, setVerifiedResourcesError] = useState<string | null>(null);

    // --- Backend-synthesized Scientific Information summary sentence
    // (multi-source: papers + verified resources) — same undefined-until-
    // fetched convention as the state above. `undefined` covers both
    // "not fetched yet" and "fetched, but the backend had no evidence to
    // synthesize from" — `scientificSummary` below can't tell those apart
    // from this alone, so it also consults `papers`/`conclusions`
    // (already-loaded-or-not) to pick the right fallback message.
    const [summaryDescription, setSummaryDescription] = useState<string | undefined>(
      ingredient.summary_description
    );

    useEffect(() => {
      if (variant !== 'standalone' || !isExpanded || papersFetchAttemptedRef.current) {
        return;
      }
      papersFetchAttemptedRef.current = true;
      setPapersLoading(true);
      setPapersError(null);
      setConclusionsLoading(true);
      setConclusionsError(null);
      setVerifiedResourcesLoading(true);
      setVerifiedResourcesError(null);

      let cancelled = false;
      fetchIngredientDetail(ingredient.id)
        .then((detail) => {
          if (!cancelled) {
            setPapers(detail.papers);
            setConclusions(detail.conclusions);
            setVerifiedResources(detail.verified_resources);
            setSummaryDescription(detail.summary_description ?? undefined);
          }
        })
        .catch((error) => {
          if (!cancelled) {
            const message =
              error instanceof Error ? error.message : 'Failed to load studies.';
            setPapersError(message);
            setConclusionsError(message);
            setVerifiedResourcesError(message);
          }
        })
        .finally(() => {
          if (!cancelled) {
            setPapersLoading(false);
            setConclusionsLoading(false);
            setVerifiedResourcesLoading(false);
          }
        });

      return () => {
        cancelled = true;
      };
    }, [variant, isExpanded, ingredient.id]);

    const handleGradeRequest = useCallback(() => {
      if (isRequestingGrade) {
        return;
      }
      setIsRequestingGrade(true);
      gradeIngredient(ingredient.id)
        .then((response) => {
          animateCardToggle();
          setIsGraded(response.is_graded);
          setGradeBadgeText(response.grade_badge_text ?? PLACEHOLDER_GRADE_VALUE);
          // The grade endpoint returns the full, freshly-updated paper
          // list — use it directly instead of triggering a second GET
          // /api/v1/ingredients/{id} round trip.
          setPapers(response.papers);
          setPapersError(null);
          papersFetchAttemptedRef.current = true;

          // Unlike `papers`, GradeIngredientResponse deliberately doesn't
          // include `conclusions`/`verified_resources`/`summary_description`
          // (see backend/app/schemas/research.py) — the Phase 5 pipeline may
          // have just synthesized new/updated conclusions, the Phase 7
          // resource lookup may have just found new official reference
          // links, and the Phase 11 ingredient-level synthesis may have
          // just produced a fresh `summary_description` from all of the
          // above, all as part of this same grade request — so re-fetch
          // ingredient detail once more to pick all three up rather than
          // leaving RecommendedUsesList/VerifiedResourcesList/the summary
          // sentence showing stale (or empty) data.
          setConclusionsLoading(true);
          setConclusionsError(null);
          setVerifiedResourcesLoading(true);
          setVerifiedResourcesError(null);
          fetchIngredientDetail(ingredient.id)
            .then((detail) => {
              setConclusions(detail.conclusions);
              setVerifiedResources(detail.verified_resources);
              setSummaryDescription(detail.summary_description ?? undefined);
            })
            .catch(() => {
              // Best-effort supplementary fetch — the grade request itself
              // already succeeded (papers/grade above are current), so a
              // failure here just means conclusions/verified resources
              // stay whatever they were before rather than surfacing a
              // second error alert.
            })
            .finally(() => {
              setConclusionsLoading(false);
              setVerifiedResourcesLoading(false);
            });
        })
        .catch((error) => {
          const message =
            error instanceof Error ? error.message : 'Unknown error occurred.';
          Alert.alert('Grading failed', message);
        })
        .finally(() => {
          setIsRequestingGrade(false);
        });
    }, [ingredient.id, isRequestingGrade]);

    /** Splices one freshly-graded paper back into local `papers` state
     * by id — passed to StudiesList as `onPaperGraded`, called after a
     * successful on-demand single-paper grade (tapping a gray "(-)"
     * badge). `papers` is owned here, not in StudiesList (see that
     * component's `onPaperGraded` prop doc), so this is the one place
     * that actually needs to update — StudiesList re-derives its sorted/
     * paginated view from this prop automatically once it changes.
     *
     * Phase 6: on-demand grading can determine a paper is actually
     * irrelevant to this ingredient (`status ===
     * PAPER_STATUS_DISCARDED_IRRELEVANT`). Every other paper-loading path
     * (fetchIngredientDetail, gradeIngredient) already excludes discarded
     * papers server-side (see app/services/search.py::
     * get_ingredient_papers), but this on-demand endpoint returns the
     * just-graded paper regardless of outcome — so a discarded paper is
     * filtered OUT of local state here instead of replaced in place,
     * keeping StudiesList/StudiesAnalysisBar consistent with what a
     * fresh fetch would show.
     */
    const handlePaperGraded = useCallback((updatedPaper: ResearchPaper) => {
      setPapers((current) => {
        if (updatedPaper.status === PAPER_STATUS_DISCARDED_IRRELEVANT) {
          return current?.filter((paper) => paper.id !== updatedPaper.id);
        }
        return current?.map((paper) => (paper.id === updatedPaper.id ? updatedPaper : paper));
      });
    }, []);

    /** One-sentence synthesis shown directly under the "Scientific
     * Information" title (Scientific Information redesign spec) — e.g.
     * "Analyzed 12 studies and 4 official resources. Average score: B
     * (78/100). Primary consensus confirms efficacy for X with strong
     * support from NIH/EFSA guidelines."
     *
     * **Priority order:**
     * 1. `summaryDescription` — the backend's Phase 11 Gemini synthesis
     *    (backend/app/services/conclusion_grader.py::
     *    synthesize_ingredient_summary), which considers BOTH graded
     *    papers AND verified official resources together. Preferred
     *    whenever present, since it's strictly richer than what the
     *    client can compute on its own.
     * 2. A client-side heuristic fallback — the same "average grade +
     *    top conclusion" sentence this component computed before Phase
     *    11 existed (the average-grade math lives in
     *    `computeAverageGrade`, utils/grades.ts) — used whenever the
     *    backend hasn't produced a `summary_description` yet (no grade
     *    request has run, the pipeline had zero papers/resources to
     *    synthesize from, or the Phase 11 Gemini call failed — see that
     *    function's docstring for when it returns nothing). This keeps
     *    the section from ever showing blank just because the richer
     *    synthesis isn't available yet. `conclusions[0]` is the
     *    top-confidence synthesized claim — `IngredientDetailResponse.
     *    conclusions` is documented as already sorted
     *    highest-confidence-first by the backend (see api.ts), so no
     *    re-sort is needed here.
     */
    const scientificSummary = useMemo(() => {
      if (summaryDescription) {
        return summaryDescription;
      }

      if (papers === undefined) {
        return 'Loading scientific analysis...';
      }
      const studyCount = papers.length;
      if (studyCount === 0) {
        return "No studies analyzed yet — tap this ingredient's grade badge above to run the research pipeline.";
      }

      const { averageGrade, averageScore } = computeAverageGrade(
        papers.map((paper) => ({ grade: paper.grade, score: paper.grade_score }))
      );
      const scoreText =
        averageGrade && averageScore !== null
          ? `Average score: ${averageGrade} (${averageScore}/100).`
          : 'Grading in progress — no average score yet.';

      const topConclusion = conclusions && conclusions.length > 0 ? conclusions[0] : null;
      const consensusText = topConclusion
        ? `Primary consensus indicates: "${topConclusion.claim_summary}"`
        : 'No synthesized consensus available yet.';

      return `Analyzed ${studyCount} ${
        studyCount === 1 ? 'study' : 'studies'
      } across databases. ${scoreText} ${consensusText}`;
    }, [summaryDescription, papers, conclusions]);

    // --- "Related Products" section pagination (standalone variant) ---
    // `ingredient.relatedProducts` is `undefined` on every current
    // caller (see RelatedProduct's own docstring for the backend gap) —
    // paginating over `[]` in that case is harmless and keeps this logic
    // identical to how it'll behave once a real data source exists.
    const [relatedProductsPage, setRelatedProductsPage] = useState(0);
    const relatedProducts = ingredient.relatedProducts ?? [];
    const relatedProductsTotalPages = Math.max(
      1,
      Math.ceil(relatedProducts.length / RELATED_PRODUCTS_PAGE_SIZE)
    );
    const relatedProductsPageItems = relatedProducts.slice(
      relatedProductsPage * RELATED_PRODUCTS_PAGE_SIZE,
      relatedProductsPage * RELATED_PRODUCTS_PAGE_SIZE + RELATED_PRODUCTS_PAGE_SIZE
    );

    return (
      <View ref={ref} style={[styles.card, isExpanded && styles.cardExpanded]}>
        <Pressable
          style={styles.headerRow}
          onPress={onToggle}
          accessibilityRole="button"
          accessibilityLabel={`${isExpanded ? 'Collapse' : 'Expand'} ${ingredient.name}`}
          accessibilityState={{ expanded: isExpanded }}
        >
          {variant === 'standalone' ? (
            <>
              <Text
                style={[styles.standaloneName, isExpanded && styles.expandedTextColor]}
                numberOfLines={2}
              >
                {ingredient.name}
              </Text>
              <View style={styles.standaloneHeaderRight}>
                <GradeBadge
                  isGraded={isGraded}
                  onRequestGrade={handleGradeRequest}
                  isExpanded={isExpanded}
                  gradeValue={gradeBadgeText}
                  isLoading={isRequestingGrade}
                />
                <Ionicons
                  name={isExpanded ? 'chevron-up' : 'chevron-down'}
                  size={18}
                  color={colors.brown}
                />
              </View>
            </>
          ) : (
            <>
              <Text style={styles.headerText} numberOfLines={2}>
                {ingredient.name} — {doseSummary}
              </Text>
              <Ionicons
                name={isExpanded ? 'chevron-up' : 'chevron-down'}
                size={18}
                color={colors.brown}
              />
            </>
          )}
        </Pressable>

        {isExpanded && variant === 'standalone' && (
          <View style={styles.expandedSection}>
            {/* "General Information" / "Grade Info" — still placeholder
                body copy (see GENERAL_INFORMATION_PLACEHOLDER/
                GRADE_INFO_PLACEHOLDER above; real content tracked in
                docs/Architecture.md's "Expandable cards" follow-up), now
                each its own independently-collapsible bordered card
                (StandaloneInfoSection) — the same outer chrome
                "Scientific Information" and "Related Products" below
                use, per the Section Visual Standardization spec. */}
            <StandaloneInfoSection title="General Information">
              <Text style={[styles.standaloneInfoText, styles.expandedTextColor]}>
                {GENERAL_INFORMATION_PLACEHOLDER}
              </Text>
            </StandaloneInfoSection>

            <StandaloneInfoSection title="Grade Info">
              <Text style={[styles.standaloneInfoText, styles.expandedTextColor]}>
                {GRADE_INFO_PLACEHOLDER}
              </Text>
            </StandaloneInfoSection>

            {/* "Scientific Information" — a synthesized one-sentence
                summary wrapping the three collapsible list panels
                (RecommendedUsesList, VerifiedResourcesList, StudiesList —
                each sorted worst-to-best... A-to-E by grade, then score,
                before their own pagination — see each component's own
                sortByGradeThenScore usage), now sharing the same
                StandaloneInfoSection chrome as every other top-level
                section here instead of its own one-off bordered View. */}
            <StandaloneInfoSection title="Scientific Information">
              <Text style={styles.scienceSectionSummary}>{scientificSummary}</Text>

              <RecommendedUsesList
                conclusions={conclusions}
                isLoading={conclusionsLoading}
                errorMessage={conclusionsError}
              />

              <VerifiedResourcesList
                resources={verifiedResources}
                isLoading={verifiedResourcesLoading}
                errorMessage={verifiedResourcesError}
              />

              <StudiesList
                papers={papers}
                isLoading={papersLoading}
                errorMessage={papersError}
                onPaperGraded={handlePaperGraded}
              />
            </StandaloneInfoSection>

            {/* "Related Products" — summary sentence + a bordered,
                paginated (5/page) list of every product this ingredient
                appears in. No backend endpoint returns that product list
                yet (see RelatedProduct's own docstring) — the box below
                renders an honest "not available" empty state rather than
                fabricating rows whenever `ingredient.relatedProducts` is
                `undefined`, which is every current caller today. */}
            <StandaloneInfoSection title="Related Products">
              <Text style={[styles.relatedProductsSummary, styles.expandedTextColor]}>
                This ingredient appears in {ingredient.productCount ?? 0}{' '}
                {ingredient.productCount === 1 ? 'product' : 'products'}.
              </Text>

              <View style={styles.relatedProductsBox}>
                {relatedProductsPageItems.length === 0 ? (
                  <Text style={styles.statusText}>
                    {ingredient.relatedProducts === undefined
                      ? 'Product list not available yet.'
                      : 'No products found for this ingredient.'}
                  </Text>
                ) : (
                  <>
                    <View style={styles.productList}>
                      {relatedProductsPageItems.map((product, index) => (
                        <View
                          key={product.id}
                          style={[
                            styles.productRow,
                            index === relatedProductsPageItems.length - 1 &&
                              styles.productRowLast,
                          ]}
                        >
                          <View style={styles.productRowLeft}>
                            {product.thumbnailUrl && (
                              <Image
                                source={{ uri: product.thumbnailUrl }}
                                style={styles.productThumb}
                                accessibilityLabel={`${product.name} thumbnail`}
                              />
                            )}
                            <View style={styles.productNameColumn}>
                              <Text style={styles.productName} numberOfLines={1}>
                                {product.name}
                              </Text>
                              {product.brand && (
                                <Text style={styles.productBrand} numberOfLines={1}>
                                  {product.brand}
                                </Text>
                              )}
                            </View>
                          </View>

                          {/* Placeholder action only — not wired to any
                              navigation/detail view yet. */}
                          <Pressable
                            style={styles.productViewButton}
                            accessibilityRole="button"
                            accessibilityLabel={`View ${product.name}`}
                            hitSlop={6}
                          >
                            <Ionicons name="search" size={18} color={colors.orange} />
                          </Pressable>
                        </View>
                      ))}
                    </View>

                    <Pagination
                      page={relatedProductsPage}
                      totalPages={relatedProductsTotalPages}
                      onPageChange={setRelatedProductsPage}
                    />
                  </>
                )}
              </View>
            </StandaloneInfoSection>
          </View>
        )}

        {isExpanded && variant === 'nested' && (
          <View style={styles.expandedSection}>
            <View style={styles.doseBlock}>
              {ingredient.amount && ingredient.unit ? (
                // Product-specific dosage — this card represents a
                // particular product's link to this ingredient.
                <View style={styles.doseRow}>
                  <Text style={styles.doseLabel}>Dosage</Text>
                  <Text style={styles.doseValue}>
                    {`${ingredient.amount} ${ingredient.unit}`}
                  </Text>
                </View>
              ) : (
                // No product-specific dosage available (standalone
                // ingredient result) — fall back to the canonical
                // ingredient's general dosage guidance placeholder.
                <View style={styles.doseRow}>
                  <Text style={styles.doseLabel}>Recommended Daily Dosage</Text>
                  <Text style={styles.doseValue}>
                    {ingredient.recommendedDailyDosage ?? 'Not available'}
                  </Text>
                </View>
              )}
              {ingredient.dailyValue && (
                <View style={styles.doseRow}>
                  <Text style={styles.doseLabel}>% Daily Value</Text>
                  <Text style={styles.doseValue}>{ingredient.dailyValue}</Text>
                </View>
              )}
              {ingredient.productName && (
                <View style={styles.doseRow}>
                  <Text style={styles.doseLabel}>Product</Text>
                  <Text style={styles.doseValue}>{ingredient.productName}</Text>
                </View>
              )}
              {typeof ingredient.productCount === 'number' && (
                <View style={styles.doseRow}>
                  <Text style={styles.doseLabel}>Found In</Text>
                  <Text style={styles.doseValue}>
                    {ingredient.productCount}{' '}
                    {ingredient.productCount === 1 ? 'product' : 'products'}
                  </Text>
                </View>
              )}
            </View>

            <View style={styles.researchPlaceholder}>
              <Text style={styles.researchPlaceholderText}>
                {ingredient.scientificData ??
                  'General compound research & metadata coming soon...'}
              </Text>
            </View>
          </View>
        )}
      </View>
    );
  }
);

IngredientCard.displayName = 'IngredientCard';

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.offWhite,
    // Thicker, dark-green-by-default border (was a thin, translucent
    // olive one) — overridden by cardExpanded (below) while open.
    borderWidth: 3,
    borderColor: colors.darkGreen,
    // Rounder, more modern feel — up from 10. Slightly smaller than
    // ProductCard's 20 (a common nested/hierarchy convention: outer
    // container rounder than what's nested inside it).
    borderRadius: 16,
    overflow: 'hidden',
  },
  // Applied on top of `card` (via a conditional array style) while the
  // ingredient is expanded — orange accent border, per spec.
  cardExpanded: {
    borderColor: colors.orange,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
    // Bumped from paddingVertical: sm/paddingHorizontal: md (8/16) for a
    // roomier, more generous card.
    padding: spacing.lg,
  },
  headerText: {
    flex: 1,
    fontSize: typography.resultCardTitle,
    fontWeight: '700',
    color: colors.brown,
  },
  // --- Standalone-variant header (name + grade badge) ---
  standaloneName: {
    flex: 1,
    fontSize: typography.resultCardTitle,
    fontWeight: '700',
    color: colors.brown,
  },
  // Groups the grade badge + chevron together on the header row's right
  // side — the wireframe only calls out "name left / badge right", but
  // the chevron (existing expand/collapse affordance) still needs a
  // home; pairing it with the badge keeps the row's two-item
  // space-between layout intact rather than adding a third column.
  standaloneHeaderRight: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
  },
  // Applied on top of every other standalone-variant text style (via a
  // conditional array style, e.g. `[styles.standaloneName, isExpanded &&
  // styles.expandedTextColor]`) while this card is expanded — forces
  // every text element inside it (name, the four info block labels/
  // placeholder bodies; GradeBadge handles its own text via the
  // `isExpanded` prop passed to it) to the palette orange, per spec. Only
  // applied to the `'standalone'` variant — nested cards are untouched.
  // The card's own background/border colors are unaffected — only text.
  expandedTextColor: {
    color: colors.orange,
  },
  expandedSection: {
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.lg,
    gap: spacing.md,
  },
  // --- Standalone-variant expanded body ---
  // Every top-level section (General Information/Grade Info/Scientific
  // Information/Related Products) now shares one bordered/collapsible
  // wrapper — see StandaloneInfoSection.tsx — instead of each hand-
  // rolling its own outer card; only section-specific *inner* content
  // styles remain here.
  //
  // Synthesized one-sentence summary directly under the Scientific
  // Information title — see `scientificSummary` above for how this text
  // is built.
  scienceSectionSummary: {
    textAlign: 'center',
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: colors.orange,
    lineHeight: 18,
  },
  standaloneInfoText: {
    fontSize: typography.resultCardLabel,
    color: `${colors.brown}AA`,
    fontStyle: 'italic',
    lineHeight: 18,
  },
  // --- "Related Products" section ---
  relatedProductsSummary: {
    fontSize: typography.resultCardLabel,
    textAlign: 'center',
  },
  // Collapsible box wrapping the product rows + pagination — per spec:
  // `1px solid #E0E0E0` / `8px` radius. Deliberately the same subtler
  // neutral border CollapsibleSection.tsx uses for the Scientific
  // Information list panels, distinct from StandaloneInfoSection's
  // bolder orange outer border this box itself sits inside.
  relatedProductsBox: {
    borderWidth: 1,
    borderColor: colors.neutralBorder,
    borderRadius: 8,
    padding: spacing.sm,
  },
  statusText: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.orange}AA`,
    textAlign: 'center',
    paddingVertical: spacing.sm,
  },
  productList: {
    gap: 0,
  },
  productRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: spacing.sm,
    paddingVertical: spacing.sm,
    borderStyle: 'dashed',
    borderBottomWidth: 1,
    borderColor: colors.orange,
  },
  productRowLast: {
    borderBottomWidth: 0,
  },
  productRowLeft: {
    flex: 1,
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    minWidth: 0,
  },
  productThumb: {
    width: 32,
    height: 32,
    borderRadius: 6,
    backgroundColor: `${colors.olive}30`,
  },
  productNameColumn: {
    flexShrink: 1,
  },
  productName: {
    fontSize: typography.resultCardLabel,
    fontWeight: '600',
    color: colors.orange,
  },
  productBrand: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.orange}AA`,
  },
  // Magnifying-glass placeholder action button — per spec, not yet wired
  // to any navigation/detail view.
  productViewButton: {
    padding: spacing.xs,
  },
  doseBlock: {
    backgroundColor: `${colors.olive}18`,
    borderRadius: 8,
    padding: spacing.md,
    gap: spacing.xs,
  },
  doseRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    gap: spacing.sm,
  },
  doseLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.brown,
  },
  doseValue: {
    fontSize: typography.resultCardLabel,
    color: colors.brown,
    flexShrink: 1,
    textAlign: 'right',
  },
  researchPlaceholder: {
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: `${colors.brown}55`,
    borderRadius: 8,
    padding: spacing.md,
  },
  researchPlaceholderText: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.brown}AA`,
    textAlign: 'center',
  },
});

export default IngredientCard;
