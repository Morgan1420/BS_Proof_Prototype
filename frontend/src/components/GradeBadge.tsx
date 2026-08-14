import React from 'react';
import { View, Text, Pressable, ActivityIndicator, StyleSheet } from 'react-native';

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
  /** Whether this item already has a grade. `false` renders an
   * "Assign Grade" button in the same spot instead of the grade pill. */
  isGraded: boolean;
  /** Called when the ungraded button is tapped. The parent card owns the
   * actual `is_graded` state and decides what "request a grade" means —
   * for standalone IngredientCard, this calls the real grading API (see
   * that component's `handleGradeRequest`); for ProductCard, it's still
   * just a local, placeholder-only flip to `true`. */
  onRequestGrade: () => void;
  /** Mirrors the parent card's own `isExpanded` — the badge/button text
   * switches to orange along with the rest of that card's text while
   * expanded, same rule as ProductCard/IngredientCard's own
   * `expandedTextColor`. */
  isExpanded: boolean;
  /** Text shown inside the pill once `isGraded` is true — e.g. a real
   * "{papers} / {papers} / {papers}" debug value from the grading API,
   * or PLACEHOLDER_GRADE_VALUE for callers with no real grade data.
   * Ignored while ungraded. */
  gradeValue: string;
  /** Shows a small spinner in place of the "Assign Grade" label and
   * disables the button while a grade request is in flight. Only
   * meaningful while `isGraded` is false. Defaults to `false` so
   * existing callers (ProductCard, whose "grading" is instant/local)
   * don't need to pass anything. */
  isLoading?: boolean;
}

/**
 * Top-right grade pill, shared by `ProductCard` and standalone
 * `IngredientCard` so both render an identical size/shape/border
 * regardless of graded state — only the content and touchability
 * differ:
 * - **Ungraded, idle** (`isGraded: false`, `isLoading: false`): a
 *   `Pressable` reading "Assign Grade".
 * - **Ungraded, loading** (`isGraded: false`, `isLoading: true`): the
 *   same pill, showing a small spinner instead of the label and no
 *   longer pressable — used while a real grade request is in flight.
 * - **Graded** (`isGraded: true`): the same-shaped pill, no longer
 *   pressable, showing `gradeValue`.
 */
const GradeBadge: React.FC<GradeBadgeProps> = ({
  isGraded,
  onRequestGrade,
  isExpanded,
  gradeValue,
  isLoading = false,
}) => {
  const textStyle = [styles.text, isExpanded && styles.textExpanded];

  if (isGraded) {
    return (
      <View style={styles.pill}>
        <Text style={textStyle}>{gradeValue}</Text>
      </View>
    );
  }

  return (
    <Pressable
      style={styles.pill}
      onPress={onRequestGrade}
      disabled={isLoading}
      accessibilityRole="button"
      accessibilityLabel={isLoading ? 'Assigning grade' : 'Assign grade'}
      accessibilityState={{ busy: isLoading, disabled: isLoading }}
    >
      {isLoading ? (
        <ActivityIndicator
          size="small"
          color={isExpanded ? colors.orange : colors.darkGreen}
        />
      ) : (
        <Text style={textStyle}>Assign Grade</Text>
      )}
    </Pressable>
  );
};

const styles = StyleSheet.create({
  // Same size/proportions/border for both graded and ungraded states —
  // only the content (and touchability) differs, per spec.
  pill: {
    borderWidth: 1.5,
    borderColor: colors.darkGreen,
    borderRadius: 15,
    paddingHorizontal: 10,
    paddingVertical: 4,
    backgroundColor: `${colors.olive}18`,
  },
  text: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.darkGreen,
  },
  // Applied on top of `text` (conditional array style) while the parent
  // card is expanded — mirrors the same pattern used for the rest of
  // that card's own text (see ProductCard/IngredientCard's
  // `expandedTextColor`).
  textExpanded: {
    color: colors.orange,
  },
});

export default GradeBadge;
