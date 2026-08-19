import React, { useState } from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';

import { colors, spacing, typography } from '../theme';

export interface StandaloneInfoSectionProps {
  /** Section title — rendered centered and bold, per spec. */
  title: string;
  /** Whether this section starts expanded. Defaults to `true` — every
   * current caller already sits behind IngredientCard's own
   * `isExpanded` gate (the whole standalone card body is only mounted
   * once the card itself is open), so collapsing again by default would
   * hide content the user just asked to see by opening the card — same
   * default/reasoning as CollapsibleSection.tsx. */
  defaultOpen?: boolean;
  children: React.ReactNode;
}

/**
 * Shared bordered/collapsible card wrapper for IngredientCard's four
 * top-level standalone-variant sections — "General Information", "Grade
 * Info", "Scientific Information", and "Related Products" — factored out
 * so all four share one visual structure (per the Section Visual
 * Standardization spec):
 *
 * - Outer container: `1px solid #E85D04` (colors.orange) border, 12px
 *   radius, 16px padding, 16px bottom margin.
 * - Section title: centered, bold, 20px (typography.sectionTitle).
 * - Collapsible toggle: tapping anywhere on the header row (title +
 *   chevron) toggles the body open/closed, with a `▼`/`▲` chevron
 *   indicator.
 *
 * Deliberately a *different* component from CollapsibleSection.tsx, not
 * a reskin of it — that component is the smaller, subtler wrapper used
 * for the three individual list panels (StudiesList/RecommendedUsesList/
 * VerifiedResourcesList) *inside* the Scientific Information section
 * (`#E0E0E0` border, left-aligned label, Ionicons chevron). This
 * component is the bolder, section-level chrome those three lists (and
 * the other three standalone blocks) all sit inside — same distinction
 * IngredientCard.tsx's `scienceSectionOuter` style already documented
 * before this pass, now shared/reused rather than hand-copied per
 * section.
 */
const StandaloneInfoSection: React.FC<StandaloneInfoSectionProps> = ({
  title,
  defaultOpen = true,
  children,
}) => {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <View style={styles.container}>
      <Pressable
        style={styles.headerRow}
        onPress={() => setIsOpen((current) => !current)}
        accessibilityRole="button"
        accessibilityLabel={`${isOpen ? 'Collapse' : 'Expand'} ${title}`}
        accessibilityState={{ expanded: isOpen }}
      >
        <Text style={styles.title}>{title}</Text>
        <Text style={styles.chevron}>{isOpen ? '▲' : '▼'}</Text>
      </Pressable>

      {isOpen && <View style={styles.body}>{children}</View>}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    borderWidth: 1,
    borderColor: colors.orange,
    borderRadius: 12,
    padding: spacing.md,
    marginBottom: spacing.md,
  },
  // `flexDirection: 'row'` is NOT React Native's default (unlike web
  // Flexbox, RN's own default is 'column') — omitting it here was the
  // bug: the title `Text` and the chevron `Text` were stacking on
  // separate lines, one above the other, instead of sitting side by
  // side. `justifyContent: 'center'` centers the (title + chevron) pair
  // together as one unit, so the title itself stays centered per spec
  // while the chevron renders immediately to its right rather than
  // pinned to the container's far edge.
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.xs,
  },
  title: {
    textAlign: 'center',
    fontSize: typography.sectionTitle,
    fontWeight: 'bold',
    color: colors.orange,
  },
  chevron: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  body: {
    marginTop: spacing.sm,
    gap: spacing.sm,
  },
});

export default StandaloneInfoSection;
