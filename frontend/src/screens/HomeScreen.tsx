import React, { useEffect } from 'react';
import {
  View,
  Text,
  Pressable,
  StyleSheet,
  ScrollView,
  useWindowDimensions,
  Platform,
} from 'react-native';
import type { TextStyle } from 'react-native';
import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { useVideoPlayer, VideoView } from 'expo-video';

import Footer from '../components/Footer';
import { colors, layout, spacing, typography } from '../theme';
import type { RootStackParamList } from '../navigation/types';

// Local background video for the Hero. A second clip
// (7710495-uhd_4096_2160_25fps.mp4) also lives in src/assets/ if you'd
// rather use the UHD version — it's ~2.5x the file size for a
// resolution bump that's wasted on a background loop, so the smaller HD
// clip is used here by default. Swap the require() below to switch.
const HERO_VIDEO = require('../assets/7685212-hd_1920_1080_24fps.mp4');

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

  // expo-av is deprecated (no patches, removed in SDK 55) — this project
  // is on SDK 57, so expo-video is the only supported path for local
  // video playback. player.loop/player.muted are expo-video's
  // equivalents of expo-av's isLooping/isMuted props.
  const player = useVideoPlayer(HERO_VIDEO, (instance) => {
    instance.loop = true;
    instance.muted = true;
    instance.volume = 0;
  });

  // Deliberately NOT calling player.play() inside the useVideoPlayer
  // setup callback above: on web, expo-video's underlying <video> element
  // isn't attached to the DOM yet when that callback runs, so play()
  // fires against a not-yet-mounted element and silently no-ops — you get
  // a static poster frame instead of playback (this is a known
  // expo-video web issue, see expo/expo#36350). Calling play() from a
  // useEffect after mount, once the VideoView has actually rendered,
  // fixes it. Native (iOS/Android) works either way.
  useEffect(() => {
    player.play();
  }, [player]);

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.content}
      // Kill the elastic overscroll/recoil at the top and bottom bounds —
      // it read as "bouncy" against the full-bleed video Hero.
      bounces={false}
      overScrollMode="never"
    >
      {/* HERO — spans the full viewport height (minHeight, so it never
          clips content on short screens / large text sizes). Stacking
          order (video -> overlay -> text/buttons) relies on render
          order for z-ordering, backed up with explicit zIndex. */}
      <View style={[styles.hero, { minHeight: windowHeight }]}>
        <VideoView
          player={player}
          style={styles.heroVideo}
          contentFit="cover"
          nativeControls={false}
        />
        <View style={styles.heroOverlay} />

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
    // Fallback color, visible behind/around the video while it loads (or
    // if playback fails) — position: relative + overflow: hidden is what
    // lets the absolutely-positioned video/overlay below fill exactly
    // this container instead of the whole screen.
    backgroundColor: colors.lightYellow,
    position: 'relative',
    overflow: 'hidden',
    paddingVertical: spacing.xl * 1.5,
    paddingHorizontal: spacing.lg,
    alignItems: 'center',
    justifyContent: 'center',
  },
  heroVideo: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    zIndex: 0,
    // Style-based pointerEvents (not the standalone prop, which RN now
    // warns is deprecated) — lets taps pass through to the buttons below.
    pointerEvents: 'none' as const,
  },
  // Dark tint over the video so the light-colored title/buttons stay
  // readable regardless of what's playing behind them.
  heroOverlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0, 0, 0, 0.35)',
    zIndex: 1,
    pointerEvents: 'none',
  },
  heroTitle: {
    // offWhite (not brown) now that this text sits over a video + dark
    // overlay instead of the flat lightYellow background — still an
    // existing palette color, just the higher-contrast one for this
    // context. A subtle text shadow adds extra separation from busy
    // video frames.
    color: colors.offWhite,
    fontSize: typography.heroTitle,
    fontWeight: '800',
    textAlign: 'center',
    marginBottom: spacing.lg,
    zIndex: 2,
    // react-native-web warns that textShadowColor/Offset/Radius are
    // deprecated in favor of the unified `textShadow` shorthand — but
    // @types/react-native doesn't know about that shorthand yet (a
    // types-vs-runtime lag, not a real error), and native RN doesn't
    // warn on the classic props at all. Split by platform: shorthand
    // (cast past the stale types) on web, classic typed props on native.
    ...(Platform.OS === 'web'
      ? ({ textShadow: '0px 2px 6px rgba(0, 0, 0, 0.45)' } as TextStyle)
      : {
          textShadowColor: 'rgba(0, 0, 0, 0.45)',
          textShadowOffset: { width: 0, height: 2 },
          textShadowRadius: 6,
        }),
  },
  heroButtons: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    justifyContent: 'center',
    gap: spacing.md,
    zIndex: 2,
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
    gap: spacing.xl,
    paddingVertical: spacing.xl,
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
