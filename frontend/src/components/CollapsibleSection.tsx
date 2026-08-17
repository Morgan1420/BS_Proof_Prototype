import React, { useState } from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';

import { colors, spacing, typography } from '../theme';

export interface CollapsibleSectionProps {
  /** Full title text already including any "(Total: N)" suffix the
   * caller wants — this wrapper doesn't know how to count its own
   * children, only how to render/toggle them. E.g. StudiesList passes
   * `List of Studies (Total: 12)`. */
  title: string;
  /** Optional one-line description shown under the title while open —
   * e.g. VerifiedResourcesList's "Authoritative reference sheets..."
   * subheading. Hidden while collapsed, same as the rest of the body. */
  subheading?: string;
  /** Whether this section starts expanded. Defaults to `true` — every
   * current caller already sits behind IngredientCard's own
   * `isExpanded` gate, so collapsing again by default would hide
   * content the user just asked to see by opening the card. */
  defaultOpen?: boolean;
  children: React.ReactNode;
}

/**
 * Shared collapsible/bordered wrapper for the three "Scientific
 * Information" list panels (StudiesList, RecommendedUsesList,
 * VerifiedResourcesList) — factored out so all three share one
 * click-title-bar-to-toggle interaction, one chevron indicator, and one
 * `1px solid #E0E0E0` / 8px-radius container border, per the Scientific
 * Information redesign spec, instead of three near-identical hand-copied
 * container/header implementations.
 *
 * `colors.neutralBorder` (`#E0E0E0`, see theme.ts) is deliberately
 * distinct from `colors.orange` — this is each individual list's own
 * subtle container border, separate from the bolder `#E85D04` border
 * wrapping the whole "Scientific Information" section these lists sit
 * inside (see IngredientCard.tsx's `scienceSectionOuter` style).
 *
 * Any Modal a caller renders (rubric/info popups) should be a sibling of
 * this component, not a child — children only mount while `isOpen` is
 * true, and a Modal needs to stay mounted (so `visible` can still toggle
 * it) even if the user collapses the section behind it.
 */
const CollapsibleSection: React.FC<CollapsibleSectionProps> = ({
  title,
  subheading,
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
        <Text style={styles.headerText}>{title}</Text>
        <Text style={styles.chevron}>{isOpen ? '▲' : '▼'}</Text>
      </Pressable>

      {isOpen && (
        <View style={styles.body}>
          {subheading && <Text style={styles.subheading}>{subheading}</Text>}
          {children}
        </View>
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    backgroundColor: `${colors.olive}18`,
    borderWidth: 1,
    borderColor: colors.neutralBorder,
    borderRadius: 8,
    overflow: 'hidden',
  },
  headerRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: spacing.sm,
    padding: spacing.md,
  },
  headerText: {
    flex: 1,
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
    letterSpacing: 0.5,
  },
  chevron: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  body: {
    paddingHorizontal: spacing.md,
    paddingBottom: spacing.md,
    gap: spacing.sm,
  },
  subheading: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.orange}CC`,
    textAlign: 'center',
  },
});

export default CollapsibleSection;
