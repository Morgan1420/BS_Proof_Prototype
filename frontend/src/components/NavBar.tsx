import React, { useState } from 'react';
import {
  View,
  Text,
  Pressable,
  StyleSheet,
  Alert,
  ActivityIndicator,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';
import { navigateTo } from '../navigation/navigationRef';
import { resetDatabase } from '../services/api';

/**
 * Persistent top navigation bar, rendered once above the Stack Navigator
 * in src/App.tsx so it stays mounted across every screen. Uses the
 * imperative `navigateTo` helper (see navigation/navigationRef.ts) since
 * it lives outside the navigator's own screen tree.
 */
const NavBar: React.FC = () => {
  const [isResetting, setIsResetting] = useState(false);

  const performReset = async (): Promise<void> => {
    setIsResetting(true);
    try {
      await resetDatabase();
      Alert.alert('Reset DB', 'Database wiped successfully');
    } catch (error) {
      console.error('Failed to reset DB:', error);
      const message =
        error instanceof Error ? error.message : 'Unknown error occurred.';
      Alert.alert('Reset failed', message);
    } finally {
      setIsResetting(false);
    }
  };

  const handleResetPress = (): void => {
    // React Native has no `Alert.confirm` — a two-button Alert.alert with
    // a destructive-styled confirm action is the platform's standard
    // confirm-dialog pattern.
    Alert.alert(
      'Reset DB',
      'Are you sure you want to completely wipe the database? This deletes all products and ingredients and cannot be undone.',
      [
        { text: 'Cancel', style: 'cancel' },
        { text: 'Delete', style: 'destructive', onPress: performReset },
      ]
    );
  };

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

          {/* Dev-only debug action: completely wipes the database via
              DELETE /api/v1/dev/mock-data. Not user-facing functionality
              — remove before shipping this anywhere real. */}
          <Pressable
            onPress={handleResetPress}
            disabled={isResetting}
            accessibilityRole="button"
            accessibilityLabel="Debug: reset database"
            hitSlop={8}
          >
            {isResetting ? (
              <ActivityIndicator size="small" color={colors.offWhite} />
            ) : (
              <Ionicons name="trash-outline" size={18} color={colors.offWhite} />
            )}
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
    color: colors.lightYellow,
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
