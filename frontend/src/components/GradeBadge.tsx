import React, { useState } from 'react';
import {
  View,
  Text,
  Pressable,
  ActivityIndicator,
  StyleSheet,
  type GestureResponderEvent,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';

/**
 * Fallback grade value for cards that don't (yet) have a real grading
 * backend to call — currently just `ProductCard`, which still flips
 * `is_graded` locally with no server round trip (product-level grading
 * isn't wired up yet; see docs/Architecture.md). Standalone
 * `IngredientCard` no longer uses this for its actual grade display — it
 * shows the real `grade_badge_text` returned by
 * POST /api/v1/ingredients/{id}/grade — but still falls back to this
 * string before that request has ever completed, so there's always
 * something reasonable to render.
 */
export const PLACEHOLDER_GRADE_VALUE = '8 / 10 / 9';

export interface GradeBadgeProps {
  /** Whether this item already has a grade. `false` renders an idle/
   * loading button in the same spot instead of the graded pill. */
  isGraded: boolean;
  /** Called when the ungraded button is tapped, OR (when `regradeLabel`
   * is provided — see below) when an already-graded badge is tapped to
   * re-trigger grading. The parent card owns the actual `is_graded`
   * state and decides what "request a grade" means — for standalone
   * IngredientCard, this calls the real grading API (see that
   * component's `handleGradeRequest`, which is safe to call again on an
   * already-graded ingredient — grade_ingredient() on the backend is
   * idempotent-safe to re-run, not gated on `is_graded` at all); for
   * ProductCard, it's still just a local, placeholder-only flip to
   * `true`. */
  onRequestGrade: () => void;
  /** Mirrors the parent card's own `isExpanded` — the badge/button text
   * switches to orange along with the rest of that card's text while
   * expanded, same rule as ProductCard/IngredientCard's own
   * `expandedTextColor`. */
  isExpanded: boolean;
  /** Text shown inside the pill once `isGraded` is true AND
   * `regradeLabel` is NOT provided — e.g. a real "{papers} / {papers} /
   * {papers}" debug value from the grading API, or
   * PLACEHOLDER_GRADE_VALUE for callers with no real grade data. Also
   * still used (as extra accessibility context only, not visible text)
   * when `regradeLabel` IS provided — see that prop's own doc. Ignored
   * while ungraded. */
  gradeValue: string;
  /** Shows a small spinner in place of the idle/regrade label and
   * disables the button while a grade request is in flight — applies to
   * BOTH State A (`isGraded: false`, idle "Assign Grade"/"Grade
   * Ingredient") and, as of Phase 41, State C's regrade affordance
   * (`isGraded: true` + `regradeLabel` set, "Grade Again"). Before Phase
   * 41 this prop was read only in the `!isGraded` branch below, so
   * re-grading an already-graded ingredient never showed any loading
   * state at all — the parent (IngredientCard.tsx) was already passing
   * `isLoading` correctly the whole time, `isGraded` just stayed `true`
   * for the full duration of a re-grade request (it only flips once the
   * response comes back), which routed every render straight into the
   * graded branch further down, which never once looked at this prop.
   * Defaults to `false` so existing callers (ProductCard, whose
   * "grading" is instant/local) don't need to pass anything. */
  isLoading?: boolean;
  /** Bumps this pill to the more prominent Grade Button design (thicker
   * `border-2` orange border, `rounded-md` corners instead of the
   * original full-pill radius, roomier `px-4 py-2`-equivalent padding,
   * brown/"orange-900"-equivalent idle text) instead of the original
   * compact darkGreen pill. Opt-in and defaults to `false` so
   * `ProductCard` (out of scope for the Grade Button redesign — its own
   * grading is still a local-only placeholder, not a real pipeline) keeps
   * rendering the exact pill it always has; only `IngredientCard.tsx`
   * passes `true`. See the styles below for the literal Tailwind ->
   * theme.ts token mapping this implements. */
  prominent?: boolean;
  /** Idle-state (ungraded, not loading) label. Defaults to `'Assign
   * Grade'` — `ProductCard`'s original, unchanged wording.
   * `IngredientCard.tsx` passes `'Grade Ingredient'`. */
  idleLabel?: string;
  /** Label shown next to the spinner while `isLoading` is true. `undefined`
   * (the default) keeps the original spinner-only loading look —
   * `IngredientCard.tsx` passes `'Grading...'`; `ProductCard` doesn't set
   * this (and doesn't pass `isLoading` at all today, so it never reaches
   * this branch regardless). */
  loadingLabel?: string;
  /** When provided, an already-graded badge stops being a static,
   * non-interactive pill and becomes a `Pressable` reading this text
   * (e.g. `'Grade Again'`) that calls `onRequestGrade` again — the
   * re-grading affordance. `undefined` (the default) preserves
   * `ProductCard`'s original behavior exactly: a plain, non-pressable
   * `View` showing `gradeValue`. `IngredientCard.tsx` passes
   * `'Grade Again'`. */
  regradeLabel?: string;
  /** Phase 41 — label shown next to the spinner while re-grading an
   * already-graded ingredient (`isGraded: true` + `regradeLabel` set +
   * `isLoading: true`), analogous to `loadingLabel` for the first-grade
   * case. Falls back to `loadingLabel` if not provided, so a caller that
   * doesn't care about the distinction can just set one label for both.
   * `IngredientCard.tsx` passes `'Re-grading...'` so the button reads
   * accurately during a "Grade Again" request instead of reusing the
   * first-grade wording. */
  regradeLoadingLabel?: string;
}

/**
 * Top-right grade pill, shared by `ProductCard` and standalone
 * `IngredientCard` so both render an identical size/shape/border
 * regardless of graded state, UNLESS `prominent` is set (IngredientCard
 * only — see that prop's own doc) — three explicit visual states, per
 * the Grade Button Styling spec:
 *
 * - **State A — ungraded, idle** (`isGraded: false`, `isLoading:
 *   false`): a `Pressable` reading `idleLabel`.
 * - **State B — grading in progress** (`isGraded: false`, `isLoading:
 *   true`): the same pill, showing a small spinner (plus `loadingLabel`,
 *   if provided) instead of the idle label, no longer pressable (`disabled`).
 * - **State C — already graded** (`isGraded: true`): if `regradeLabel` is
 *   provided, a `Pressable` reading it (tapping re-triggers grading via
 *   `onRequestGrade` again); otherwise the original static, non-pressable
 *   pill showing `gradeValue`.
 * - **State C-loading — re-grading in progress** (Phase 41; `isGraded:
 *   true`, `regradeLabel` set, AND `isLoading: true`): the same pressable
 *   pill, but showing a spinner (plus `regradeLoadingLabel`/`loadingLabel`,
 *   if provided) instead of the regrade label, and `disabled` — same
 *   shape as State B, just reachable from the graded side. Before Phase
 *   41 this state didn't exist: the graded branch never looked at
 *   `isLoading` at all, so re-grading silently kept showing the idle
 *   "Grade Again" pill for the whole request.
 *
 * **No CSS `transition-all` equivalent.** React Native's `StyleSheet`
 * has no built-in property-transition/animation concept the way CSS
 * does — the `prominent` pill's hover tint (see `onHoverIn`/`onHoverOut`
 * below) therefore snaps instantly rather than fading, a deliberate,
 * documented simplification rather than reaching for `Animated`/
 * `react-native-reanimated` over a purely cosmetic hover tint. Hover
 * itself is a genuine, harmless no-op on native touch (RN's `Pressable`
 * only fires `onHoverIn`/`onHoverOut` for hover-capable pointers, i.e.
 * react-native-web) — same pattern already established in
 * `IngredientCard.tsx`'s own locked-card hover label.
 */
const GradeBadge: React.FC<GradeBadgeProps> = ({
  isGraded,
  onRequestGrade,
  isExpanded,
  gradeValue,
  isLoading = false,
  prominent = false,
  idleLabel = 'Assign Grade',
  loadingLabel,
  regradeLabel,
  regradeLoadingLabel,
}) => {
  const [isHovered, setIsHovered] = useState(false);

  const textStyle = [
    styles.text,
    prominent && styles.textProminent,
    isExpanded && styles.textExpanded,
  ];
  const pillStyle = [
    styles.pill,
    prominent && styles.pillProminent,
    prominent && isHovered && styles.pillHovered,
  ];
  const hoverHandlers = prominent
    ? { onHoverIn: () => setIsHovered(true), onHoverOut: () => setIsHovered(false) }
    : undefined;

  // This pill is always nested inside a larger tappable header row
  // (ProductCard/IngredientCard's own accordion-toggle Pressable) — on
  // native iOS/Android, RN's touch-responder system already resolves a
  // tap to whichever Pressable is deepest under the finger, so the
  // header's own `onPress` never fires just because this nested one was
  // tapped. React Native Web does NOT replicate that isolation: it's a
  // real DOM click event, which bubbles up through ancestor `onClick`
  // handlers by default same as any other nested `<button>`-in-`<div>`
  // markup — without stopping it here, tapping "Grade Ingredient"/"Grade
  // Again" on web would ALSO fire the header row's own `onPress` (toggle
  // expand/collapse) on the same tap, fighting the button's own action.
  // `event.stopPropagation()` is a genuine no-op on native (RN's
  // GestureResponderEvent still exposes it, but native's responder
  // system doesn't use DOM bubbling in the first place), so this is safe
  // to call unconditionally on every platform.
  const handlePress = (event: GestureResponderEvent): void => {
    event.stopPropagation();
    onRequestGrade();
  };

  if (isGraded) {
    if (!regradeLabel) {
      // Original behavior — a static, non-interactive pill. Every
      // current ProductCard usage stays exactly here (it never passes
      // `regradeLabel`).
      return (
        <View style={pillStyle}>
          <Text style={textStyle}>{gradeValue}</Text>
        </View>
      );
    }

    // State C — already graded, re-grade affordance (IngredientCard.tsx
    // only). A small "refresh" glyph next to the label reinforces that
    // this is a RE-grade action, not the original "assign a first grade"
    // one, on top of the wording change alone.
    //
    // Phase 41 — State C-loading. `isGraded` stays `true` for the WHOLE
    // duration of a re-grade request (the parent only flips it once the
    // response comes back — see IngredientCard.tsx's `handleGradeRequest`),
    // so this branch, not the `!isGraded` one below, is what actually
    // renders while a "Grade Again" request is in flight. Mirrors State
    // B's spinner/disabled treatment exactly, just with the graded pill's
    // background/icon instead of the idle one.
    if (isLoading) {
      // A disabled Pressable (not a plain View) — same "still technically
      // the pressable element, just non-interactive" shape as State B
      // below, rather than a different component type for what's
      // conceptually the same "in-flight" state.
      return (
        <Pressable
          style={[pillStyle, prominent && styles.pillGraded]}
          disabled
          accessibilityRole="button"
          accessibilityLabel={regradeLoadingLabel ?? loadingLabel ?? 'Re-grading'}
          accessibilityState={{ busy: true, disabled: true }}
        >
          <View style={styles.loadingRow}>
            <ActivityIndicator
              size="small"
              color={isExpanded ? colors.orange : prominent ? colors.brown : colors.darkGreen}
            />
            {(regradeLoadingLabel ?? loadingLabel) && (
              <Text style={textStyle}>{regradeLoadingLabel ?? loadingLabel}</Text>
            )}
          </View>
        </Pressable>
      );
    }

    return (
      <Pressable
        style={[pillStyle, prominent && styles.pillGraded]}
        onPress={handlePress}
        {...hoverHandlers}
        accessibilityRole="button"
        accessibilityLabel={`Grade again. Current grade: ${gradeValue}.`}
        hitSlop={4}
      >
        <Ionicons
          name="refresh"
          size={13}
          color={isExpanded ? colors.orange : prominent ? colors.brown : colors.darkGreen}
        />
        <Text style={textStyle}>{regradeLabel}</Text>
      </Pressable>
    );
  }

  return (
    <Pressable
      style={pillStyle}
      onPress={handlePress}
      disabled={isLoading}
      {...hoverHandlers}
      accessibilityRole="button"
      accessibilityLabel={isLoading ? (loadingLabel ?? 'Assigning grade') : idleLabel}
      accessibilityState={{ busy: isLoading, disabled: isLoading }}
    >
      {isLoading ? (
        <View style={styles.loadingRow}>
          <ActivityIndicator
            size="small"
            color={isExpanded ? colors.orange : prominent ? colors.brown : colors.darkGreen}
          />
          {loadingLabel && <Text style={textStyle}>{loadingLabel}</Text>}
        </View>
      ) : (
        <Text style={textStyle}>{idleLabel}</Text>
      )}
    </Pressable>
  );
};

const styles = StyleSheet.create({
  // Same size/proportions/border for both graded and ungraded states —
  // only the content (and touchability) differs, per spec. This is the
  // ORIGINAL compact pill — still exactly what ProductCard renders
  // (`prominent` defaults to `false`).
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    borderWidth: 1.5,
    borderColor: colors.darkGreen,
    borderRadius: 15,
    paddingHorizontal: 10,
    paddingVertical: 4,
    backgroundColor: `${colors.olive}18`,
  },
  // --- `prominent` (IngredientCard.tsx only) — Grade Button Styling
  // spec, translated from Tailwind classes onto theme.ts's existing
  // palette (no Tailwind compiler in this Expo/React Native app — same
  // deviation this session already applied to every other Tailwind-
  // flavored UI spec):
  //   border-2 border-orange-500  -> borderWidth: 2, borderColor: colors.orange
  //   px-4 py-2                   -> paddingHorizontal: spacing.md, paddingVertical: spacing.sm
  //   rounded-md                  -> borderRadius: 6 (replaces the original full-pill radius)
  //   font-semibold text-orange-900 -> see `textProminent` below
  //   hover:bg-orange-100         -> see `pillHovered` below
  pillProminent: {
    borderWidth: 2,
    borderColor: colors.orange,
    borderRadius: 6,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    backgroundColor: 'transparent',
  },
  // `hover:bg-orange-100` equivalent — only applied on top of
  // `pillProminent` (State A/loading — see `pillStyle`'s composition
  // above; State C's graded pill uses `pillGraded` instead, its own,
  // slightly stronger tint, so an already-graded pill never looks
  // identical to a plain idle hover).
  pillHovered: {
    backgroundColor: `${colors.orange}18`,
  },
  // State C (already graded, re-grade affordance) background — a
  // permanent, slightly stronger tint than the hover state above, so a
  // graded pill reads as visually distinct from an idle one even before
  // any hover/press interaction, on top of its different icon/label.
  pillGraded: {
    backgroundColor: `${colors.olive}20`,
  },
  text: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.darkGreen,
  },
  // `font-semibold text-orange-900` — font-semibold is 600 (overrides
  // the base pill's 700); `text-orange-900` maps to `colors.brown`, this
  // app's existing dark warm-toned text color (see IngredientFilter.tsx
  // for the same mapping applied to the same Tailwind token).
  textProminent: {
    fontWeight: '600',
    color: colors.brown,
  },
  // Applied on top of `text`/`textProminent` (conditional array style,
  // ordered last so it always wins) while the parent card is expanded —
  // mirrors the same pattern used for the rest of that card's own text
  // (see ProductCard/IngredientCard's `expandedTextColor`).
  textExpanded: {
    color: colors.orange,
  },
  loadingRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
  },
});

export default GradeBadge;
