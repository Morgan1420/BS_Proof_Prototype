import React from 'react';
import {
  View,
  Text,
  Pressable,
  StyleSheet,
  ScrollView,
  useWindowDimensions,
} from 'react-native';
import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

import Footer from '../components/Footer';
import { colors, layout, spacing, typography } from '../theme';
import type { RootStackParamList } from '../navigation/types';

type HomeScreenNavigationProp = NativeStackNavigationProp<
  RootStackParamList,
  'HomeScreen'
>;

const INFO_TITLE = 'Cut Through the Marketing Hype';
const INFO_BODY =
  "Supplement labels are often packed with proprietary blends, confusing " +
  'dosage units, and misleading marketing claims. BSProof uses vision AI ' +
  'to instantly scan your label, extract every active ingredient, and ' +
  "present clear, structured dosage data—giving you complete transparency " +
  "over what you're putting into your body.";

/**
 * Marketing home page: full-viewport-height Hero with primary/secondary
 * CTAs, a two-column info section explaining the product, and the shared
 * Footer.
 */
const HomeScreen: React.FC = () => {
  const navigation = useNavigation<HomeScreenNavigationProp>();
  const { height: windowHeight } = useWindowDimensions();

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      {/* HERO — spans the full viewport height (minHeight, so it never
          clips content on short screens / large text sizes) */}
      <View style={[styles.hero, { minHeight: windowHeight }]}>
        <Text style={styles.heroTitle}>BS Proof</Text>
        <View style={styles.heroButtons}>
          <Pressable
            style={[styles.heroButton, styles.scanButton]}
            onPress={() => navigation.navigate('ScanScreen')}
            accessibilityRole="button"
            accessibilityLabel="Scan Supplement"
          >
            <Text style={styles.scanButtonText}>Scan Supplement</Text>
          </Pressable>
          <Pressable
            style={[styles.heroButton, styles.libraryButton]}
            onPress={() => navigation.navigate('LibraryScreen')}
            accessibilityRole="button"
            accessibilityLabel="Supplement Library"
          >
            <Text style={styles.libraryButtonText}>Supplement Library</Text>
          </Pressable>
        </View>
      </View>

      {/* INFO — two-column layout: title + body text on the left, image
          placeholder on the right, side-by-side */}
      <View style={styles.infoSection}>
        <View style={styles.infoLeftColumn}>
          <Text style={styles.infoTitle}>{INFO_TITLE}</Text>
          <Text style={styles.infoBody}>{INFO_BODY}</Text>
        </View>
        <View
          style={styles.infoImagePlaceholder}
          accessibilityLabel="Image placeholder"
        />
      </View>

      <Footer />
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.offWhite,
  },
  content: {
    flexGrow: 1,
  },
  hero: {
    backgroundColor: colors.lightYellow,
    paddingVertical: spacing.xl * 1.5,
    paddingHorizontal: spacing.lg,
    alignItems: 'center',
    justifyContent: 'center',
  },
  heroTitle: {
    color: colors.brown,
    fontSize: typography.heroTitle,
    fontWeight: '800',
    textAlign: 'center',
    marginBottom: spacing.lg,
  },
  heroButtons: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    justifyContent: 'center',
    gap: spacing.md,
  },
  heroButton: {
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.lg,
    borderRadius: 10,
    minWidth: 180,
    alignItems: 'center',
  },
  scanButton: {
    backgroundColor: colors.orange,
  },
  scanButtonText: {
    color: colors.offWhite,
    fontSize: typography.buttonLabel,
    fontWeight: '700',
  },
  libraryButton: {
    backgroundColor: colors.yellow,
  },
  libraryButtonText: {
    color: colors.brown,
    fontSize: typography.buttonLabel,
    fontWeight: '700',
  },
  // Hero (above) stays full-width/0% padding per the layout rule; this is
  // the screen's "main body container" that gets the global 20% inset.
  infoSection: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.lg,
    paddingVertical: spacing.lg,
    paddingHorizontal: layout.screenHorizontalPadding,
  },
  infoLeftColumn: {
    flex: 1,
    minWidth: 200,
    gap: spacing.sm,
  },
  infoTitle: {
    fontSize: typography.sectionTitle,
    fontWeight: '700',
    color: colors.brown,
    marginBottom: spacing.sm,
    textAlign: 'left',
  },
  infoBody: {
    fontSize: typography.body,
    color: colors.brown,
    lineHeight: 22,
  },
  infoImagePlaceholder: {
    flex: 1,
    minWidth: 200,
    minHeight: 220,
    backgroundColor: colors.olive,
    borderRadius: 12,
  },
});

export default HomeScreen;
