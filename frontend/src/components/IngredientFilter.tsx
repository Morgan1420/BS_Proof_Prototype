import React, { useState } from 'react';
import { View, Text, Pressable, Modal, StyleSheet } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import {
  FILTER_GROUPS,
  FILTER_LABELS,
  type FilterType,
} from '../utils/ingredientFilters';

export interface IngredientFilterProps {
  /** Currently applied filter — `'ALL'` is both the top option and the
   * default fallback, per spec. */
  activeFilter: FilterType;
  /** Called with the newly selected filter when the user taps an option
   * in the popover; the popover closes itself immediately after. */
  onChange: (filter: FilterType) => void;
  /** How many currently-loaded results match `activeFilter` right now —
   * rendered in the active-filter indicator badge (e.g. "Filter:
   * Vitamins (10)"). Omitted/`undefined` while results are still
   * loading, in which case the badge shows the label alone, no count. */
  activeCount?: number;
}

/**
 * Filter button + popover for `ResultsScreen.tsx`'s results list —
 * replaces that screen's old static, non-functional `Ionicons
 * name="filter"` placeholder icon.
 *
 * Implemented as a `Modal` (transparent, fade) rather than a true
 * anchored/measured dropdown — same pattern this codebase's info/rubric
 * modals already use throughout (`StudiesList.tsx`,
 * `VerifiedResourcesList.tsx`, `ScientificConclusionsList.tsx`), chosen
 * for the same reason here: React Native has no built-in
 * anchor-to-trigger positioning primitive, and a full-screen `Modal`
 * with a dismiss-on-backdrop-tap overlay is this app's established way
 * of avoiding a hand-rolled `measure()`/absolute-position calculation.
 *
 * Styled from `theme.ts`'s existing strict palette, not the task's
 * literal Tailwind class names (`bg-amber-50`/`border-orange-300`/
 * `text-orange-900`) — this is a React Native/Expo app with no Tailwind
 * compiler available (same deviation reasoning as every other
 * Tailwind-flavored spec this session, e.g. the Phase 31 grade-modal
 * standardization). Mapped onto the closest existing tokens instead of
 * introducing new hex values, per theme.ts's own "every color used
 * anywhere in the UI should come from this file" rule:
 * `bg-amber-50` -> `colors.offWhite` (the app's cream background),
 * `border-orange-300` -> `colors.orange`, `text-orange-900` ->
 * `colors.brown` (the app's existing dark warm text color).
 */
const IngredientFilter: React.FC<IngredientFilterProps> = ({
  activeFilter,
  onChange,
  activeCount,
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const isDefault = activeFilter === 'ALL';

  const handleSelect = (filter: FilterType): void => {
    onChange(filter);
    setIsOpen(false);
  };

  return (
    <>
      <Pressable
        style={[styles.triggerButton, !isDefault && styles.triggerButtonActive]}
        onPress={() => setIsOpen(true)}
        accessibilityRole="button"
        accessibilityLabel={
          isDefault
            ? 'Filter ingredients'
            : `Filter: ${FILTER_LABELS[activeFilter]}${
                typeof activeCount === 'number' ? ` (${activeCount})` : ''
              }. Tap to change.`
        }
      >
        <Ionicons
          name="filter"
          size={16}
          color={isDefault ? colors.brown : colors.orange}
        />
        {!isDefault && (
          <Text style={styles.triggerButtonText} numberOfLines={1}>
            {FILTER_LABELS[activeFilter]}
            {typeof activeCount === 'number' ? ` (${activeCount})` : ''}
          </Text>
        )}
      </Pressable>

      <Modal
        visible={isOpen}
        transparent
        animationType="fade"
        onRequestClose={() => setIsOpen(false)}
      >
        <Pressable
          style={styles.backdrop}
          onPress={() => setIsOpen(false)}
          accessibilityLabel="Close filter menu"
          accessibilityRole="button"
        >
          {/* Inner Pressable with no-op onPress: stops a tap on the
              popover card itself from bubbling to the backdrop Pressable
              behind it and closing the menu — same "swallow the tap"
              pattern this codebase's other modals use for their content
              card vs. backdrop. */}
          <Pressable style={styles.popover} onPress={() => undefined}>
            <Text style={styles.popoverTitle}>Filter Ingredients</Text>

            {FILTER_GROUPS.map((group, groupIndex) => (
              <View key={group.label || `group-${groupIndex}`} style={styles.group}>
                {group.label ? (
                  <Text style={styles.groupLabel}>{group.label}</Text>
                ) : null}
                {group.options.map((option) => {
                  const isActive = option === activeFilter;
                  return (
                    <Pressable
                      key={option}
                      style={[styles.option, isActive && styles.optionActive]}
                      onPress={() => handleSelect(option)}
                      accessibilityRole="button"
                      accessibilityState={{ selected: isActive }}
                    >
                      <Text
                        style={[styles.optionText, isActive && styles.optionTextActive]}
                      >
                        {FILTER_LABELS[option]}
                      </Text>
                      {isActive && (
                        <Ionicons name="checkmark" size={16} color={colors.orange} />
                      )}
                    </Pressable>
                  );
                })}
              </View>
            ))}
          </Pressable>
        </Pressable>
      </Modal>
    </>
  );
};

const styles = StyleSheet.create({
  triggerButton: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.xs,
    borderWidth: 1,
    borderColor: 'transparent',
    borderRadius: 8,
    paddingHorizontal: spacing.xs,
    paddingVertical: 4,
    maxWidth: 180,
  },
  // Active (non-default) indicator state, per spec: "Show an active
  // indicator badge on the Filter button when a non-default filter is
  // selected."
  triggerButtonActive: {
    borderColor: colors.orange,
    backgroundColor: `${colors.orange}18`,
  },
  triggerButtonText: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
    flexShrink: 1,
  },
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(0, 0, 0, 0.25)',
    alignItems: 'flex-end',
    padding: spacing.md,
  },
  popover: {
    marginTop: 56,
    width: 260,
    maxWidth: '100%',
    backgroundColor: colors.offWhite,
    borderWidth: 1,
    borderColor: colors.orange,
    borderRadius: 12,
    padding: spacing.sm,
    gap: spacing.sm,
  },
  popoverTitle: {
    fontSize: typography.resultCardTitle,
    fontWeight: '700',
    color: colors.brown,
    textAlign: 'center',
    marginBottom: spacing.xs,
  },
  group: {
    gap: 2,
  },
  groupLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: `${colors.brown}99`,
    textTransform: 'uppercase',
    paddingHorizontal: spacing.xs,
    paddingTop: spacing.xs,
  },
  option: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    borderRadius: 8,
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.sm,
  },
  optionActive: {
    backgroundColor: `${colors.orange}18`,
  },
  optionText: {
    fontSize: typography.resultCardLabel,
    color: colors.brown,
  },
  optionTextActive: {
    fontWeight: '700',
    color: colors.orange,
  },
});

export default IngredientFilter;
