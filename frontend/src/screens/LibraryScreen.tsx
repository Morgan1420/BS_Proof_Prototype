import React from 'react';
import { View, Text, StyleSheet } from 'react-native';

import Footer from '../components/Footer';
import { colors, spacing, typography } from '../theme';

/**
 * Placeholder screen for the future Supplement Library. The NavBar is
 * already rendered above this screen by src/App.tsx (it's persistent
 * across the whole app), so this only needs the central content + Footer.
 */
const LibraryScreen: React.FC = () => {
  return (
    <View style={styles.screen}>
      <View style={styles.content}>
        <Text style={styles.title}>Content Library - Coming Soon.</Text>
      </View>
      <Footer />
    </View>
  );
};

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.offWhite,
    justifyContent: 'space-between',
  },
  content: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.lg,
  },
  title: {
    fontSize: typography.sectionTitle,
    fontWeight: '700',
    color: colors.brown,
    textAlign: 'center',
  },
});

export default LibraryScreen;
