import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { View, Text, Pressable, StyleSheet, Alert, Image } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import { animateCardToggle } from '../utils/animations';
import { computeAverageGrade, sortByGradeThenScore } from '../utils/grades';
import GradeBadge, { PLACEHOLDER_GRADE_VALUE } from './GradeBadge';
import StudiesList from './StudiesList';
import ScientificConclusionsList from './ScientificConclusionsList';
import VerifiedResourcesList from './VerifiedResourcesList';
import StandaloneInfoSection from './StandaloneInfoSection';
import Pagination from './Pagination';
import {
  fetchIngredientDetail,
  gradeIngredient,
  PAPER_STATUS_DISCARDED_IRRELEVANT,
} from '../services/api';
import type {
  ScientificConclusion,
  PaperConclusion,
  ResearchPaper,
  VerifiedResource,
  GeneralInfo,
  GeneralInfoField,
} from '../services/api';

/** Max related products shown per page — same "5 per list page" rule
 * every other Scientific Information list panel uses (see
 * StudiesList.tsx/ScientificConclusionsList.tsx/VerifiedResourcesList.tsx's
 * own PAGE_SIZE constants). */
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
   * see backend/app/models/research.py). Phase 29: no longer rendered as
   * its own list panel — the "Recommended Uses List" component that used
   * to display these (`RecommendedUsesList.tsx`) was removed from the
   * standalone variant's "Scientific information" block to eliminate
   * duplication/confusion against `scientificConclusions`/
   * ScientificConclusionsList below, which is now the single
   * user-facing source of truth for "what is this ingredient good for."
   * `conclusions` itself is still fetched and kept here — it feeds
   * `scientificSummary`'s client-side fallback sentence (used only when
   * the backend hasn't produced a `summary_description` yet, see that
   * `useMemo` further down this file) and remains genuinely necessary
   * backend-side too: `conclusion_grader.py::synthesize_ingredient_summary`
   * (Stage 2) consumes the same PaperConclusion rows as input evidence
   * when synthesizing `scientific_conclusions` itself, so this data isn't
   * dead — only its dedicated UI list panel was removed. Same
   * "undefined = not loaded yet, lazily fetched on first expand"
   * convention as `papers` above. */
  conclusions?: PaperConclusion[];
  /** Every stored VerifiedResource for this ingredient (Phase 7 — see
   * backend/app/models/research.py), rendered by VerifiedResourcesList in
   * the standalone variant's "Scientific information" block. Same
   * "undefined = not loaded yet, lazily fetched on first expand"
   * convention as `papers`/`conclusions` above. */
  verified_resources?: VerifiedResource[];
  /** Phase 23, renamed Phase 24 from `recommendedUses` — the
   * ingredient-level, Multi-Source Confidence Rubric-scored
   * `scientific_conclusions` array (see ScientificConclusion's own
   * doc-comment in src/services/api.ts for why this is a DIFFERENT thing
   * from `conclusions` above despite the similar naming), rendered by the
   * separate ScientificConclusionsList component in the standalone
   * variant's "Scientific information" block. Guaranteed (Phase 24 Direct
   * Injection Safety Net, server-side) to include every
   * VerifiedResource.extracted_conclusions entry in some form. Same
   * "undefined = not loaded yet, lazily fetched on first expand"
   * convention as `papers`/`conclusions`/`verified_resources` above. */
  scientificConclusions?: ScientificConclusion[];
  /** Phase 33 — General Information (Description + Daily Dosage), each
   * independently resolved under a strict Grade A/B-only source
   * hierarchy — see GeneralInfo's own doc-comment in services/api.ts.
   * Rendered by the standalone variant's "General Information" section
   * below, replacing the old static placeholder text. Same
   * "undefined = not loaded yet, lazily fetched on first expand"
   * convention as `papers`/`conclusions`/`verified_resources`/
   * `scientificConclusions` above. */
  generalInfo?: GeneralInfo;
  /** Phase 19/20 note: this component does NOT own any "(i)" info-modal
   * markup itself — it is a pure passthrough that hands `papers`/
   * `verified_resources` straight to StudiesList/VerifiedResourcesList
   * below (see the JSX further down this file), and each of those two
   * components independently implements its own `activeInfoModalItem`
   * Modal. The `extracted_conclusions` field added to `ResearchPaper`/
   * `VerifiedResource` (src/services/api.ts, Phase 19), and the
   * `extraction_failure_reason` field added to `VerifiedResource`
   * (Phase 20 — the notice box shown when a resource's
   * `extracted_conclusions` comes back empty), therefore flow through to
   * those modals automatically once the item objects carry them — no
   * transformation logic was needed (or added) in this file. See
   * StudiesList.tsx's and VerifiedResourcesList.tsx's own "Extracted
   * Conclusions" modal sections for the actual rendering.
   *
   * Phase 22 note: the same is true of `aligned_conclusions` (added to
   * `VerifiedResource` — src/services/api.ts) and its colored Agrees/
   * Contradicts/Distinct-New badges — that field flows through this same
   * untouched passthrough straight to VerifiedResourcesList's info modal,
   * which owns the badge rendering (see utils/alignment.ts for the
   * color/label mapping). Nothing in this file changed for Phase 22.
   *
   * Phase 23 note: same pure-passthrough story again for
   * `scientificConclusions` (renamed Phase 24 from `recommendedUses`) —
   * handed straight to the ScientificConclusionsList component below,
   * which owns its own score-breakdown/sources_summary modal rendering
   * entirely, including for any Phase 24 Python-injected entries. */
  /** Every product this ingredient appears in, for the "Related
   * Products" section. `undefined` (the only value any current caller
   * ever sets — see RelatedProduct's own docstring) means "no backend
   * data source for this yet"; a real caller would use an empty array to
   * mean "confirmed zero products" instead. Only meaningful for the
   * `'standalone'` variant. */
  relatedProducts?: RelatedProduct[];
}

/**
 * One Phase 33 General Information card (Description or Daily Dosage) —
 * a small, self-contained presentational component rather than inline
 * JSX repeated twice, since both fields share identical layout logic:
 * title, resolved `text`, then a `[Grade X - Source Name]` badge — per
 * the Implementation Specs' "source badge showing name+grade" call-out.
 *
 * **Phase 37 — strict conditional rendering.** This component is now
 * ONLY ever mounted by its caller (below) when `field.is_available &&
 * field.text` is already known to be true — see the "General
 * Information" section's own conditional rendering, which decides
 * per-field whether to render this card AT ALL rather than delegating
 * that decision in here. Through Phase 36 this component rendered
 * unconditionally, with internal branches for "still loading"/"fetch
 * failed"/"resolved but unavailable"/"not generated yet" — the strict
 * per-field spec ("Otherwise, DO NOT render the ... container") replaces
 * all four of those with either rendering the real card or rendering
 * nothing at all; a single combined empty-state message (see
 * `GENERAL_INFO_EMPTY_STATE_MESSAGE` below) covers every "nothing to
 * show yet" case once, for the whole General Information section, rather
 * than each field separately explaining why it's empty. `field` is
 * therefore a required (non-optional) prop now — the defensive `return
 * null` below exists only in case a future caller ever regresses that
 * invariant, not because this component expects to hit it in practice.
 */
interface GeneralInfoCardProps {
  title: string;
  field: GeneralInfoField;
}

const GeneralInfoCard: React.FC<GeneralInfoCardProps> = ({ title, field }) => {
  if (!field.is_available || !field.text) {
    return null;
  }

  const sourceBadge =
    field.source_grade && field.source_name
      ? `Grade ${field.source_grade} - ${field.source_name}`
      : null;

  return (
    <View style={styles.generalInfoCard}>
      <Text style={styles.generalInfoCardTitle}>{title}</Text>
      <Text style={styles.generalInfoCardBody}>{field.text}</Text>
      {sourceBadge && (
        <View style={styles.generalInfoSourceBadge}>
          <Text style={styles.generalInfoSourceBadgeText}>{sourceBadge}</Text>
        </View>
      )}
    </View>
  );
};

/**
 * "Scientific Claims" sub-card (Phase 37) — the third General
 * Information card, alongside Description/Daily Dosage above. Renders a
 * short intro sentence plus a bullet list of `ScientificConclusion`
 * claims, each tagged with its letter grade.
 *
 * **STRICT FILTER, enforced by the caller, not in here.** Per spec,
 * ONLY Grade A/B claims may ever reach this card — `claims` is expected
 * to already be filtered (see `highGradeClaims` in the main component
 * body below) and non-empty (the caller only mounts this card when
 * `claims.length > 0`, same "caller decides whether to render at all"
 * pattern `GeneralInfoCard` above now follows). Sorted A-before-B (then
 * by `total_score` descending) via the shared `sortByGradeThenScore`
 * helper (`utils/grades.ts`) — same ordering rule already used
 * throughout this app's other graded lists (`StudiesList.tsx`,
 * `VerifiedResourcesList.tsx`, `ScientificConclusionsList.tsx` itself).
 *
 * Deliberately does NOT paginate or open its own info/rubric modal, the
 * way the full `ScientificConclusionsList` panel (Scientific Information
 * section, further down this file) does — this is meant as a short,
 * curated "headline claims" highlight reel inside General Information,
 * not a second full copy of that list. A claim's full score breakdown/
 * sources/justification is still one tap away via `ScientificConclusionsList`
 * itself further down the same expanded card.
 */
interface ScientificClaimsCardProps {
  claims: ScientificConclusion[];
}

const ScientificClaimsCard: React.FC<ScientificClaimsCardProps> = ({ claims }) => (
  <View style={styles.generalInfoCard}>
    <Text style={styles.generalInfoCardTitle}>Scientific Claims</Text>
    <Text style={styles.scientificClaimsIntro}>
      Key evidence-backed claims derived from Grade A and B research:
    </Text>
    <View style={styles.scientificClaimsList}>
      {claims.map((claim, index) => (
        <View
          key={`${claim.claim}-${index}`}
          style={[
            styles.scientificClaimRow,
            index === claims.length - 1 && styles.scientificClaimRowLast,
          ]}
        >
          <Text style={styles.scientificClaimBullet}>{'•'}</Text>
          <Text style={styles.scientificClaimText}>{claim.claim}</Text>
          <View style={styles.generalInfoSourceBadge}>
            <Text style={styles.generalInfoSourceBadgeText}>
              Grade {claim.confidence_grade}
            </Text>
          </View>
        </View>
      ))}
    </View>
  </View>
);

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

/**
 * Phase 37 — single combined empty-state message for the whole "General
 * Information" section, shown once (in place of every sub-card) when
 * NONE of Description/Daily Dosage/Scientific Claims has any Grade A/B
 * data available. Replaces the old per-field "Not generated yet"/
 * "unavailable" messaging (see `GeneralInfoCard`'s own doc-comment above
 * for why: the new spec renders a card only when it has real content, or
 * renders nothing for it at all — there's no longer a per-field message
 * to show), and replaces the Phase 33 `GENERAL_INFO_UNAVAILABLE_NOTICE`
 * constant this file used to define for that purpose.
 */
const GENERAL_INFO_EMPTY_STATE_MESSAGE =
  'No high-confidence (Grade A or B) general information or scientific claims are currently available for this ingredient.';

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

    // --- PaperConclusion data (Phase 5) — same undefined/loading/error/
    // fetch-once-per-mount conventions as `papers` above. Phase 29: no
    // longer rendered as its own list panel (the old "Recommended Uses
    // List" / RecommendedUsesList.tsx component was removed — see
    // `conclusions`'s own doc-comment on the Ingredient interface above
    // for why); this state is kept purely because `scientificSummary`'s
    // fallback sentence below still reads `conclusions[0]` as its "top
    // consensus" fallback when no `summary_description` exists yet.
    // Populated by the same GET /api/v1/ingredients/{id} call as `papers` (one fetch, both
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

    // --- ScientificConclusionsList data (standalone variant's "Scientific
    // information" block, Phase 11/23, renamed Phase 24 from
    // "MultiSourceUsesList data") — same undefined/loading/error/
    // fetch-once-per-mount conventions as `papers`/`conclusions`/
    // `verifiedResources` above. Populated by the same
    // GET /api/v1/ingredients/{id} call as all three (one fetch, every
    // field on the response) — see the shared effect below. See
    // ScientificConclusion's own doc-comment in services/api.ts for why
    // this is a DIFFERENT array from `conclusions` despite the similar
    // naming, and for the Phase 24 Direct Injection Safety Net guarantee.
    const [scientificConclusions, setScientificConclusions] = useState<
      ScientificConclusion[] | undefined
    >(ingredient.scientificConclusions);
    const [scientificConclusionsLoading, setScientificConclusionsLoading] = useState(false);
    const [scientificConclusionsError, setScientificConclusionsError] = useState<
      string | null
    >(null);

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

    // --- General Information data (Phase 33) — same undefined/loading/
    // error/fetch-once-per-mount conventions as `scientificConclusions`
    // above. Populated by the same GET /api/v1/ingredients/{id} call as
    // every other Scientific Information field — see the shared effect
    // below. `undefined` = not fetched yet; a fetched-but-null
    // `detail.general_info` (no grade request has run this extraction
    // yet) is normalized to `undefined` too, so the render logic only
    // needs one "not generated yet" check.
    const [generalInfo, setGeneralInfo] = useState<GeneralInfo | undefined>(
      ingredient.generalInfo
    );
    const [generalInfoLoading, setGeneralInfoLoading] = useState(false);
    const [generalInfoError, setGeneralInfoError] = useState<string | null>(null);

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
      setScientificConclusionsLoading(true);
      setScientificConclusionsError(null);
      setGeneralInfoLoading(true);
      setGeneralInfoError(null);

      let cancelled = false;
      fetchIngredientDetail(ingredient.id)
        .then((detail) => {
          if (!cancelled) {
            setPapers(detail.papers);
            setConclusions(detail.conclusions);
            setVerifiedResources(detail.verified_resources);
            setScientificConclusions(detail.scientific_conclusions);
            setSummaryDescription(detail.summary_description ?? undefined);
            setGeneralInfo(detail.general_info ?? undefined);
            // Phase 38 — keep isGraded/gradeBadgeText in sync with the
            // real, freshly-fetched DB value too, not just whatever the
            // card was initially seeded with (ingredient.is_graded, from
            // the search/browse result list — see toIngredient's
            // doc-comment in ResultsScreen.tsx) or last set by this
            // card's own handleGradeRequest response. Belt-and-suspenders
            // alongside the search-endpoint fix: this is the one place
            // that can never be stale, since it's a direct read of
            // GET /api/v1/ingredients/{id} for this exact card.
            setIsGraded(detail.is_graded);
            setGradeBadgeText(detail.grade_badge_text ?? PLACEHOLDER_GRADE_VALUE);
          }
        })
        .catch((error) => {
          if (!cancelled) {
            const message =
              error instanceof Error ? error.message : 'Failed to load studies.';
            setPapersError(message);
            setConclusionsError(message);
            setVerifiedResourcesError(message);
            setScientificConclusionsError(message);
            setGeneralInfoError(message);
          }
        })
        .finally(() => {
          if (!cancelled) {
            setPapersLoading(false);
            setConclusionsLoading(false);
            setVerifiedResourcesLoading(false);
            setScientificConclusionsLoading(false);
            setGeneralInfoLoading(false);
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
          // include `conclusions`/`verified_resources`/`summary_description`/
          // `scientific_conclusions` (see backend/app/schemas/research.py)
          // — the Phase 5 pipeline may have just synthesized new/updated
          // conclusions, the Phase 7 resource lookup may have just found
          // new official reference links, and the Phase 11/23/24
          // ingredient-level synthesis may have just produced a fresh
          // `summary_description`/rubric-scored `scientific_conclusions`
          // (including any Phase 24 Direct Injection Safety Net entries)
          // from all of the above, all as part of this same grade request
          // — so re-fetch ingredient detail once more to pick all four up
          // rather than leaving RecommendedUsesList/VerifiedResourcesList/
          // ScientificConclusionsList/the summary sentence showing stale
          // (or empty) data.
          setConclusionsLoading(true);
          setConclusionsError(null);
          setVerifiedResourcesLoading(true);
          setVerifiedResourcesError(null);
          setScientificConclusionsLoading(true);
          setScientificConclusionsError(null);
          setGeneralInfoLoading(true);
          setGeneralInfoError(null);
          fetchIngredientDetail(ingredient.id)
            .then((detail) => {
              setConclusions(detail.conclusions);
              setVerifiedResources(detail.verified_resources);
              setScientificConclusions(detail.scientific_conclusions);
              setSummaryDescription(detail.summary_description ?? undefined);
              setGeneralInfo(detail.general_info ?? undefined);
            })
            .catch(() => {
              // Best-effort supplementary fetch — the grade request itself
              // already succeeded (papers/grade above are current), so a
              // failure here just means conclusions/verified resources/
              // scientific conclusions/general info stay whatever they
              // were before rather than surfacing a second error alert.
            })
            .finally(() => {
              setConclusionsLoading(false);
              setVerifiedResourcesLoading(false);
              setScientificConclusionsLoading(false);
              setGeneralInfoLoading(false);
            });
        })
        .catch((error) => {
          const message =
            error instanceof Error ? error.message : 'Unknown error occurred.';
          // Explicit, structured console logging ahead of the
          // user-facing alert below — gradeIngredient() (src/services/
          // api.ts) already turns a network failure or a non-2xx HTTP
          // response (including the backend's own `detail` message on a
          // 502 from a pipeline-level GradingError — see
          // app/api/routes.py's grade_ingredient route) into a plain
          // Error with a readable `.message`; logging the original
          // `error` object here (not just `message`) preserves the full
          // stack trace in the browser/Metro console for debugging,
          // beyond what the one-line Alert below can show.
          console.error('[Grading Error]:', error);
          // This app's established error-surfacing convention — there's
          // no toast library dependency here, and every other failure
          // path in this component (see the effect below) already uses
          // Alert.alert for exactly this purpose; kept consistent rather
          // than introducing a one-off toast just for this call site.
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

    // --- Ungraded card lock (standalone variant only) ---
    // An ungraded standalone ingredient has no papers/resources/
    // conclusions/general info to show yet — Scientific Information is
    // meaningless before a grade request has run at all — so the
    // accordion itself is locked shut until `isGraded` flips true, rather
    // than letting the user expand into an empty/placeholder-only body.
    // Deliberately reads the local `isGraded` state (not
    // `ingredient.is_graded` directly) since that state is what actually
    // stays in sync with a real grade request completing (see
    // `handleGradeRequest` above) — `ingredient.is_graded` is only ever
    // its initial seed value. Nested-variant cards are never locked: only
    // the standalone variant has a real backend grading concept at all
    // (see `is_graded`'s own doc-comment on the Ingredient interface).
    const isLocked = variant === 'standalone' && !isGraded;
    // Web-only progressive enhancement — RN's Pressable exposes
    // onHoverIn/onHoverOut for hover-capable pointers (react-native-web)
    // and simply never fires them on a touch-only device, so this is safe
    // to wire unconditionally. The lock affordance itself (icon + inline
    // label below) is deliberately NOT hover-gated, unlike the literal
    // spec's "show a lock icon... when hovering" — touch devices have no
    // hover state at all, so gating visibility on it would make the
    // affordance undiscoverable on mobile. Hover here only adds a small
    // extra emphasis (label color shift) for mouse users, on top of an
    // always-visible base affordance.
    const [isHeaderHovered, setIsHeaderHovered] = useState(false);

    // --- General Information strict Grade A/B gating (Phase 37) ---
    // Per spec: each of the three General Information sub-cards
    // (Description/Daily Dosage/Scientific Claims) renders ONLY when it
    // genuinely has Grade A/B content — never a loading/error/
    // "unavailable" placeholder card. `generalInfo.description`/
    // `.daily_dosage` are already resolved server-side under a strict
    // Grade A/B-only hierarchy (see backend/app/services/
    // general_info_extractor.py) — `is_available` alone is sufficient
    // here, no separate grade check needed for those two.
    const isDescriptionAvailable = Boolean(
      generalInfo?.description.is_available && generalInfo.description.text
    );
    const isDosageAvailable = Boolean(
      generalInfo?.daily_dosage.is_available && generalInfo.daily_dosage.text
    );
    // Scientific Claims' STRICT FILTER is applied here, client-side —
    // `scientificConclusions` (the full, ungated array feeding the
    // Scientific Information section's own ScientificConclusionsList
    // further down this file) is NOT itself grade-filtered server-side,
    // so this is the one place that actually enforces "ONLY Grade A or
    // B" for this specific card. Sorted A-before-B/score-descending via
    // the same shared helper every other graded list in this app uses,
    // so a user scanning this short list sees the strongest claims
    // first.
    const highGradeClaims = useMemo(() => {
      if (!scientificConclusions) {
        return [];
      }
      return sortByGradeThenScore(
        scientificConclusions.filter(
          (item) => item.confidence_grade === 'A' || item.confidence_grade === 'B'
        ),
        (item) => item.confidence_grade,
        (item) => item.total_score
      );
    }, [scientificConclusions]);
    const hasHighGradeClaims = highGradeClaims.length > 0;
    const hasAnyGeneralInfoContent =
      isDescriptionAvailable || isDosageAvailable || hasHighGradeClaims;
    // Guards the section's empty-state fallback from flashing on
    // screen while General Information/Scientific Claims data is still
    // in flight (either fetch hasn't resolved yet) — showing "no
    // high-confidence information available" before the real answer is
    // even back would be misleading, not just momentarily empty.
    const isGeneralInfoDataLoading = generalInfoLoading || scientificConclusionsLoading;

    return (
      <View ref={ref} style={[styles.card, isExpanded && styles.cardExpanded]}>
        <Pressable
          style={styles.headerRow}
          // Deliberately NOT `disabled={isLocked}` — a genuinely
          // "unresponsive Grade button" bug traced back to exactly this:
          // React Native Web's Pressable applies real CSS `pointer-events:
          // none` to a `disabled` element, which (unlike native RN's own
          // touch-responder negotiation, where an ancestor's `disabled`
          // state doesn't block a nested Pressable from independently
          // claiming a touch) DOES cascade to every descendant in the DOM
          // by default — silently swallowing clicks on the nested
          // GradeBadge Pressable too, precisely while the card is locked
          // (`isLocked` is only ever true for an UNGRADED ingredient —
          // exactly the state a user needs to tap "Grade Ingredient" in).
          // A conditional `onPress` achieves the same "locked = tapping
          // the header does nothing" behavior without touching
          // pointer-events at all, so it can never block anything nested
          // inside this row. `accessibilityState.disabled` below still
          // reports the locked state for screen readers/testing, purely
          // as metadata — it has no bearing on this pointer-events issue.
          onPress={isLocked ? undefined : onToggle}
          onHoverIn={() => setIsHeaderHovered(true)}
          onHoverOut={() => setIsHeaderHovered(false)}
          accessibilityRole="button"
          accessibilityLabel={
            isLocked
              ? `${ingredient.name} is locked. Grade ingredient to unlock scientific analysis.`
              : `${isExpanded ? 'Collapse' : 'Expand'} ${ingredient.name}`
          }
          accessibilityState={{ expanded: isExpanded, disabled: isLocked }}
        >
          {variant === 'standalone' ? (
            <>
              <View style={styles.standaloneNameColumn}>
                <Text
                  style={[styles.standaloneName, isExpanded && styles.expandedTextColor]}
                  numberOfLines={2}
                >
                  {ingredient.name}
                </Text>
                {isLocked && (
                  <Text
                    style={[
                      styles.lockedLabel,
                      isHeaderHovered && styles.lockedLabelHovered,
                    ]}
                  >
                    Grade ingredient to unlock scientific analysis
                  </Text>
                )}
              </View>
              <View style={styles.standaloneHeaderRight}>
                <GradeBadge
                  isGraded={isGraded}
                  onRequestGrade={handleGradeRequest}
                  isExpanded={isExpanded}
                  gradeValue={gradeBadgeText}
                  isLoading={isRequestingGrade}
                  prominent
                  idleLabel="Grade Ingredient"
                  loadingLabel="Grading..."
                  regradeLabel="Grade Again"
                  regradeLoadingLabel="Re-grading..."
                />
                {isLocked ? (
                  <Ionicons name="lock-closed" size={18} color={colors.brown} />
                ) : (
                  <Ionicons
                    name={isExpanded ? 'chevron-up' : 'chevron-down'}
                    size={18}
                    color={colors.brown}
                  />
                )}
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
            {/* "General Information" (Phase 33, strict grade gating +
                Scientific Claims added Phase 37) — up to three sub-cards,
                each independently resolved under a strict Grade A/B-only
                bar: Description + Daily Dosage (Healthy Adult) from
                backend/app/services/general_info_extractor.py's
                verified-resource-then-paper hierarchy, and Scientific
                Claims from this ingredient's own `scientific_conclusions`
                array, client-side filtered down to Grade A/B entries only
                (see `highGradeClaims` above). Per spec, a sub-card renders
                ONLY when it actually has Grade A/B content — there is no
                loading/error/"unavailable" placeholder card anymore (see
                `GeneralInfoCard`'s own doc-comment for the Phase 36 ->
                Phase 37 change) — and if NONE of the three have anything,
                a single combined empty-state message renders in their
                place (`GENERAL_INFO_EMPTY_STATE_MESSAGE`) rather than
                three separate "nothing here" boxes. Shares the same
                StandaloneInfoSection outer chrome as "Scientific
                Information"/"Related Products" below, per the Section
                Visual Standardization spec.

                Phase 37 also removed the old "Grade Info" section that
                used to sit here (a permanent, never-implemented
                placeholder — "Detailed breakdown of safety, efficacy, and
                purity grade criteria placeholder...") per the "Remove
                Grade Info from Ingredient Card" spec — its stated purpose
                is now superseded by this section's real Description/
                Scientific Claims content. The header itself was already
                clean of any letter-grade (A/B/C/D/E) badge before this
                phase — `GradeBadge` above is a status/action control
                ("Grade Ingredient"/"Grading..."/"Grade Again"), not a
                grade-value display, and the spec's own "status/actions"
                carve-out keeps it exactly as-is. */}
            <StandaloneInfoSection title="General Information">
              {isDescriptionAvailable && (
                <GeneralInfoCard title="Description" field={generalInfo!.description} />
              )}
              {isDosageAvailable && (
                <GeneralInfoCard
                  title="Daily Dosage (Healthy Adult)"
                  field={generalInfo!.daily_dosage}
                />
              )}
              {hasHighGradeClaims && <ScientificClaimsCard claims={highGradeClaims} />}

              {!hasAnyGeneralInfoContent &&
                (isGeneralInfoDataLoading ? (
                  <Text style={[styles.standaloneInfoText, styles.expandedTextColor]}>
                    Loading general information...
                  </Text>
                ) : (
                  <View style={styles.generalInfoCard}>
                    <Text style={styles.generalInfoEmptyText}>
                      {GENERAL_INFO_EMPTY_STATE_MESSAGE}
                    </Text>
                  </View>
                ))}
            </StandaloneInfoSection>

            {/* "Scientific Information" — a synthesized one-sentence
                summary wrapping the three collapsible list panels
                (ScientificConclusionsList, VerifiedResourcesList,
                StudiesList — each sorted worst-to-best... A-to-E by
                grade, then score, before their own pagination — see each
                component's own sortByGradeThenScore usage), now sharing
                the same StandaloneInfoSection chrome as every other
                top-level section here instead of its own one-off
                bordered View.

                Phase 29: the old "Recommended Uses List"
                (RecommendedUsesList.tsx, Phase 5's PaperConclusion-based
                list) was removed from here — it visually duplicated
                ScientificConclusionsList below (both rendered a list of
                "what this ingredient may help with," just backed by
                different data: raw per-paper PaperConclusion rows vs. the
                rubric-scored, multi-source-synthesized
                scientific_conclusions array) and caused UI confusion.
                ScientificConclusionsList is now the single source of
                truth for that kind of claim. Its underlying
                PaperConclusion data (`conclusions` state above) is still
                fetched — it remains genuine backend input evidence for
                the synthesis that produces scientific_conclusions (see
                conclusion_grader.py::synthesize_ingredient_summary) and
                still backs scientificSummary's client-side fallback
                sentence above — only its own dedicated list panel was
                removed. RecommendedUsesList.tsx itself is left in place,
                deprecated (not deleted) and unimported, same convention
                already used for MultiSourceUsesList.tsx (Phase 24). */}
            <StandaloneInfoSection title="Scientific Information">
              <Text style={styles.scienceSectionSummary}>{scientificSummary}</Text>

              {/* Phase 23, renamed Phase 24 — Multi-Source Confidence
                  Rubric-scored Ingredient.scientific_conclusions, a
                  DIFFERENT array from the `conclusions` state above
                  despite similar naming — see ScientificConclusion's
                  doc-comment in services/api.ts for the full distinction.
                  Guaranteed (Phase 24 Direct Injection Safety Net) to
                  include every VerifiedResource.extracted_conclusions
                  entry in some form. Phase 29: this is now the ONLY
                  "what is this ingredient good for" list panel rendered
                  here — see the section comment above. */}
              <ScientificConclusionsList
                scientificConclusions={scientificConclusions}
                isLoading={scientificConclusionsLoading}
                errorMessage={scientificConclusionsError}
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
  // Wraps `standaloneName` (+ the locked-state inline label, when
  // present) so the name and its lock explanation stack vertically while
  // the pair together still claims the header row's remaining flexible
  // space, same as `standaloneName` alone used to (`flex: 1` moved here).
  standaloneNameColumn: {
    flex: 1,
  },
  standaloneName: {
    fontSize: typography.resultCardTitle,
    fontWeight: '700',
    color: colors.brown,
  },
  // Inline "locked" explanation (per spec: "tooltip or subtle inline
  // label") — always visible under the name while `isLocked`, not
  // hover-gated (see the `isLocked`/`isHeaderHovered` comment at this
  // component's top for why touch devices need this to be discoverable
  // without a hover state at all). `isHeaderHovered` brightens it
  // slightly as a small progressive enhancement for mouse users only.
  lockedLabel: {
    marginTop: 2,
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.brown}99`,
  },
  lockedLabelHovered: {
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
  // --- "General Information" section (Phase 33; restyled + extended
  // Phase 37) — one warm cream/tan card per field (Description / Daily
  // Dosage / Scientific Claims). Phase 37's "Standardized Container
  // Background Styling" spec called for a shared muted tan tint across
  // all three sub-sections matching the old Scientific Conclusions box
  // aesthetic (spec's own example values: `bg-[#ede7d7]` /
  // `bg-amber-100/50` with `rounded-xl p-4 border border-amber-200/60`,
  // `text-amber-900`/`text-orange-950` type). This codebase has no
  // Tailwind compiler (RN/Expo, not web Tailwind), so those class names
  // are mapped onto theme.ts's existing alpha-blended token convention
  // instead of introducing new raw hex values: `${colors.yellow}26` (~15%
  // opacity warm tan wash, closest existing token to `#ede7d7`/amber-100)
  // for the background, `${colors.orange}55` for the border, and
  // `colors.brown` (the palette's one dark orange/brown text token) for
  // both heading and body — satisfying the spec's "warm dark orange/brown
  // heading and body text" requirement without inventing an `amber-900`-
  // equivalent color. Distinct from StandaloneInfoSection's own bolder
  // orange outer border this card sits inside.
  generalInfoCard: {
    backgroundColor: `${colors.yellow}26`,
    borderWidth: 1,
    borderColor: `${colors.orange}55`,
    borderRadius: 12,
    padding: spacing.md,
    gap: spacing.xs,
  },
  generalInfoCardTitle: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.brown,
  },
  generalInfoCardBody: {
    fontSize: typography.resultCardLabel,
    color: colors.brown,
    lineHeight: 18,
  },
  // Source attribution badge, e.g. "Grade A - Health Canada" — only
  // rendered when the field actually resolved to a real source (never
  // shown alongside the unavailable notice). Small/self-contained pill,
  // same orange-on-transparent convention as this file's other badges.
  generalInfoSourceBadge: {
    alignSelf: 'flex-start',
    borderWidth: 1,
    borderColor: colors.orange,
    borderRadius: 6,
    paddingHorizontal: spacing.xs,
    paddingVertical: 2,
  },
  generalInfoSourceBadgeText: {
    fontSize: typography.resultCardLabel - 2,
    fontWeight: '600',
    color: colors.orange,
  },
  // Single combined empty-state fallback (Phase 37) — rendered instead of
  // the three field cards above only when NONE of Description/Daily
  // Dosage/Scientific Claims has any Grade A/B content at all. Reuses
  // `generalInfoCard`'s warm tan container so it doesn't look like a
  // distinct/broken state, just an honest "nothing to show yet" card.
  generalInfoEmptyText: {
    fontSize: typography.resultCardLabel,
    color: colors.brown,
    lineHeight: 18,
    fontStyle: 'italic',
  },
  // --- "Scientific Claims" sub-card (Phase 37) — sits inside the same
  // `generalInfoCard` warm-tan container, listing only Grade A/B
  // scientific conclusions (see `highGradeClaims` in the component body).
  scientificClaimsIntro: {
    fontSize: typography.resultCardLabel,
    color: colors.brown,
    lineHeight: 18,
  },
  scientificClaimsList: {
    gap: spacing.sm,
  },
  scientificClaimRow: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: spacing.xs,
    paddingBottom: spacing.sm,
    borderBottomWidth: 1,
    borderBottomColor: `${colors.orange}33`,
  },
  scientificClaimRowLast: {
    paddingBottom: 0,
    borderBottomWidth: 0,
  },
  scientificClaimBullet: {
    fontSize: typography.resultCardLabel,
    color: colors.orange,
    lineHeight: 18,
  },
  scientificClaimText: {
    flex: 1,
    fontSize: typography.resultCardLabel,
    color: colors.brown,
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
