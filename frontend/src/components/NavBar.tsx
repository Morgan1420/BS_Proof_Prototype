import React, { useEffect, useState } from 'react';
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
import { navigateTo, navigationRef } from '../navigation/navigationRef';
import { resetDatabase } from '../services/api';

/**
 * Persistent top navigation bar, rendered once above the Stack Navigator
 * in src/App.tsx so it stays mounted across every screen. Uses the
 * imperative `navigateTo` helper (see navigation/navigationRef.ts) since
 * it lives outside the navigator's own screen tree.
 *
 * **Always visible — no hide-on-scroll.** A Phase 12 pass previously
 * added scroll-driven show/hide behavior here (a `scrollDirection.ts`
 * pub/sub module, an `Animated.Value`-driven translateY/height
 * collapse); that has been fully reverted per an explicit follow-up
 * request. This component now has no scroll listeners, no scroll
 * direction state, and no animated transforms — it renders unconditionally
 * at the top of the screen on every render, same as before Phase 12.
 *
 * The revert request describes this in web CSS terms (`position:
 * 'sticky'`, `top: 0`, `zIndex: 1000`) — those don't map onto this
 * component 1:1 since it isn't a DOM element inside a scrolling parent:
 * it's rendered once in `App.tsx` as a sibling *above* `<Stack.Navigator>`
 * (not inside any screen's own `ScrollView`/`FlatList`), so it was never
 * capable of scrolling out of view in the first place — no `position`
 * trick is needed to keep it "always visible" on the default (non-Home)
 * variant below, since normal document flow above a separately-scrolling
 * sibling already guarantees that. The one variant that *does* use
 * positioning is `safeAreaHome` (unchanged from before Phase 12):
 * `position: 'absolute'` + `zIndex: 100` so the bar floats, permanently
 * visible, over HomeScreen's full-bleed video Hero instead of pushing it
 * down — the closest RN equivalent of the requested `zIndex: 1000` /
 * always-on-top behavior.
 */
const NavBar: React.FC = () => {
  const [isResetting, setIsResetting] = useState(false);

  // Same reasoning as navigateTo() above: NavBar renders outside the
  // Stack Navigator's own screen tree, so hooks like useRoute()/
  // useNavigationState() (which need a Navigator's React context) aren't
  // available here — only the imperative navigationRef is. Defaults to
  // 'HomeScreen' to match the Stack's initialRouteName, so the very
  // first render (before the container reports "ready") already shows
  // the correct (overlay) style instead of flashing the solid bar first.
  const [currentRouteName, setCurrentRouteName] = useState<
    string | undefined
  >('HomeScreen');

  useEffect(() => {
    const updateRouteName = (): void => {
      setCurrentRouteName(navigationRef.getCurrentRoute()?.name);
    };
    if (navigationRef.isReady()) {
      updateRouteName();
    }
    const unsubscribe = navigationRef.addListener('state', updateRouteName);
    return unsubscribe;
  }, []);

  const isHomeScreen = currentRouteName === 'HomeScreen';

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
    <SafeAreaView
      edges={['top']}
      style={[styles.safeArea, isHomeScreen && styles.safeAreaHome]}
    >
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
  // Default (non-Home): a normal, in-flow, opaque bar — always rendered,
  // never animated/hidden. Background lives here (not on `container`) so
  // the Home overlay variant below can override it in one place.
  safeArea: {
    backgroundColor: colors.darkGreen,
  },
  // Home-only: floats over the top of the Hero instead of pushing it
  // down. Removing NavBar from normal flow (position: 'absolute') is
  // what lets the Hero occupy the full viewport height behind it — no
  // change needed on HomeScreen's own layout for this to work. zIndex/
  // elevation keep it permanently on top of the video, never hidden.
  safeAreaHome: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    zIndex: 100,
    elevation: 100, // Android needs elevation, not just zIndex, to stack above the Hero.
    backgroundColor: 'rgba(53, 90, 53, 0.8)', // darkGreen (#355A35) @ 80% opacity
  },
  container: {
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
