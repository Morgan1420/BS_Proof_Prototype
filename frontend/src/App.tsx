import React from 'react';
import { StyleSheet, View } from 'react-native';
import { StatusBar } from 'expo-status-bar';
import { NavigationContainer } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import NavBar from './components/NavBar';
import HomeScreen from './screens/HomeScreen';
import ScanScreen from './screens/ScanScreen';
import LibraryScreen from './screens/LibraryScreen';
import ResultsScreen from './screens/ResultsScreen';
import { navigationRef } from './navigation/navigationRef';
import type { RootStackParamList } from './navigation/types';
import { colors } from './theme';

const Stack = createNativeStackNavigator<RootStackParamList>();

/**
 * Root app component. Renders a persistent NavBar above the Stack
 * Navigator (rather than per-screen headers), so it stays mounted and
 * visible across every screen. Native stack headers are disabled since
 * NavBar replaces them.
 */
const App: React.FC = () => {
  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
      <NavigationContainer ref={navigationRef}>
        <View style={styles.root}>
          <NavBar />
          <View style={styles.stackWrapper}>
            <Stack.Navigator
              initialRouteName="HomeScreen"
              screenOptions={{ headerShown: false }}
            >
              <Stack.Screen name="HomeScreen" component={HomeScreen} />
              <Stack.Screen name="ScanScreen" component={ScanScreen} />
              <Stack.Screen name="LibraryScreen" component={LibraryScreen} />
              <Stack.Screen name="ResultsScreen" component={ResultsScreen} />
            </Stack.Navigator>
          </View>
        </View>
      </NavigationContainer>
    </SafeAreaProvider>
  );
};

const styles = StyleSheet.create({
  root: {
    flex: 1,
    backgroundColor: colors.offWhite,
  },
  stackWrapper: {
    flex: 1,
  },
});

export default App;
