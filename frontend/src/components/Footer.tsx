import React from 'react';
import { View, Text, StyleSheet } from 'react-native';

import { colors, spacing, typography } from '../theme';

/** Persistent footer placeholder, reused across every screen. */
const Footer: React.FC = () => {
  return (
    <View style={styles.container}>
      <Text style={styles.text}>
        {'©'} {new Date().getFullYear()} BSProof
      </Text>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    // Explicitly stretch to fill the parent's width regardless of the
    // parent's own alignItems setting (e.g. a screen that centers its
    // main content shouldn't shrink-wrap the footer along with it).
    alignSelf: 'stretch',
    width: '100%',
    backgroundColor: colors.darkGreen,
    paddingVertical: spacing.lg,
    paddingHorizontal: spacing.md,
    alignItems: 'center',
  },
  text: {
    color: colors.offWhite,
    fontSize: typography.body,
  },
});

export default Footer;
