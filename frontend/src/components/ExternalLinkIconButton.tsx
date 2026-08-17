import React from 'react';
import { Pressable, Alert, Linking, Platform, StyleSheet } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing } from '../theme';

export interface ExternalLinkIconButtonProps {
  url: string;
  accessibilityLabel: string;
}

/**
 * Shared "🌐" external-link row action — the third of the Scientific
 * Information redesign's three standard per-row action icons (grade
 * badge, info icon, this one). Factored out of StudiesList.tsx (which
 * already opened `ResearchPaper.source_url` this way via a plain
 * Ionicons globe + `Linking.openURL`) so VerifiedResourcesList.tsx's row
 * action for `VerifiedResource.url` renders an identical icon/
 * interaction instead of its own separate "View Resource ↗" text link.
 * RecommendedUsesList.tsx deliberately never renders this — a
 * PaperConclusion is a synthesized cross-paper claim, not a single
 * external page, so there's no one URL for it to open (per spec:
 * "Recommended uses list elements MUST NOT include the website icon").
 *
 * On web, renders a genuine `<a href target="_blank" rel="noopener
 * noreferrer">` wrapping the icon via a raw `React.createElement` —
 * React Native's `Pressable`/`Text` don't reliably pass an
 * `href`/`target`/`rel` triplet through react-native-web to the
 * underlying DOM node, and "opens...in a new tab (target=\"_blank\")" is
 * an explicit, literal requirement here (this exact pattern previously
 * lived only in VerifiedResourcesList.tsx's own `ViewResourceLink`; now
 * shared). On native (iOS/Android), a Pressable calling
 * `Linking.openURL`, same as StudiesList always did.
 */
const ExternalLinkIconButton: React.FC<ExternalLinkIconButtonProps> = ({
  url,
  accessibilityLabel,
}) => {
  if (Platform.OS === 'web') {
    return React.createElement(
      'a',
      {
        href: url,
        target: '_blank',
        rel: 'noopener noreferrer',
        style: webLinkStyle,
        'aria-label': accessibilityLabel,
      },
      React.createElement(Ionicons, { name: 'globe-outline', size: 20, color: colors.orange })
    );
  }

  return (
    <Pressable
      style={styles.button}
      onPress={() => {
        Linking.openURL(url).catch(() => {
          Alert.alert('Could not open link', url);
        });
      }}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      hitSlop={6}
    >
      <Ionicons name="globe-outline" size={20} color={colors.orange} />
    </Pressable>
  );
};

// Plain CSS-in-JS object for the web-only <a> tag above — React Native's
// StyleSheet output isn't guaranteed to apply cleanly to a raw DOM node
// created via React.createElement('a', ...), so this uses real CSS
// property names/values directly, same approach VerifiedResourcesList's
// old ViewResourceLink used for its own webLinkStyle.
const webLinkStyle: Record<string, string | number> = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: spacing.xs,
  cursor: 'pointer',
  textDecoration: 'none',
};

const styles = StyleSheet.create({
  button: {
    padding: spacing.xs,
  },
});

export default ExternalLinkIconButton;
