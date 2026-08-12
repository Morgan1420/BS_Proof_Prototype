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
