import React from 'react';
import { View, Text, Pressable, StyleSheet, ScrollView } from 'react-native';
import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

import Footer from '../components/Footer';
import { colors, spacing, typography } from '../theme';
import type { RootStackParamList } from '../navigation/types';

type HomeScreenNavigationProp = NativeStackNavigationProp<
  RootStackParamList,
  'HomeScreen'
>;

/** Repeated placeholder copy for the info section's left column. */
const PLACEHOLDER_PARAGRAPH = 'BSProof '.repeat(24).trim();

/**
 * Marketing home page: Hero with primary/secondary CTAs, an info section
 * explaining the product, and the shared Footer.
 */
const HomeScreen: React.FC = () => {
  const navigation = useNavigation<HomeScreenNavigationProp>();

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      {/* HERO */}
      <View style={styles.hero}>
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

      {/* INFO */}
      <View style={styles.infoSection}>
        <Text style={styles.infoTitle}>Why BSProof?</Text>
        <View style={styles.infoColumns}>
          <View style={styles.infoTextColumn}>
            <Text style={styles.infoParagraph}>{PLACEHOLDER_PARAGRAPH}</Text>
            <Text style={styles.infoParagraph}>{PLACEHOLDER_PARAGRAPH}</Text>
            <Text style={styles.infoParagraph}>{PLACEHOLDER_PARAGRAPH}</Text>
          </View>
          <View
            style={styles.infoImagePlaceholder}
            accessibilityLabel="Image placeholder"
          />
        </View>
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
  infoSection: {
    padding: spacing.lg,
  },
  infoTitle: {
    fontSize: typography.sectionTitle,
    fontWeight: '700',
    color: colors.brown,
    marginBottom: spacing.md,
    textAlign: 'left',
  },
  infoColumns: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: spacing.lg,
  },
  infoTextColumn: {
    flex: 1,
    minWidth: 200,
    gap: spacing.sm,
  },
  infoParagraph: {
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
