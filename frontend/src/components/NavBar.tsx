import React from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { colors, spacing, typography } from '../theme';
import { navigateTo } from '../navigation/navigationRef';

/**
 * Persistent top navigation bar, rendered once above the Stack Navigator
 * in src/App.tsx so it stays mounted across every screen. Uses the
 * imperative `navigateTo` helper (see navigation/navigationRef.ts) since
 * it lives outside the navigator's own screen tree.
 */
const NavBar: React.FC = () => {
  return (
    <SafeAreaView edges={['top']} style={styles.safeArea}>
      <View style={styles.container}>
        <Pressable
          onPress={() => navigateTo('HomeScreen')}
          accessibilityRole="button"
          accessibilityLabel="BSProof home"
          hitSlop={8}
        >
          <Text style={styles.logo}>BSProof</Text>
        </Pressable>

        <View style={styles.links}>
          <Pressable
            onPress={() => navigateTo('ScanScreen')}
            accessibilityRole="button"
            accessibilityLabel="Scan"
            hitSlop={8}
          >
            <Text style={styles.linkText}>Scan</Text>
          </Pressable>
          <Pressable
            onPress={() => navigateTo('LibraryScreen')}
            accessibilityRole="button"
            accessibilityLabel="Supplement Library"
            hitSlop={8}
          >
            <Text style={styles.linkText}>Supplement Library</Text>
          </Pressable>
        </View>
      </View>
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  safeArea: {
    backgroundColor: colors.darkGreen,
  },
  container: {
    backgroundColor: colors.darkGreen,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  logo: {
    color: colors.brown,
    fontSize: typography.navBarLogo,
    fontWeight: '800',
  },
  links: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.lg,
  },
  linkText: {
    color: colors.offWhite,
    fontSize: typography.navBarLink,
    fontWeight: '600',
  },
});

export default NavBar;
