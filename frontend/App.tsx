import React from 'react';
import { StatusBar } from 'expo-status-bar';
import HomeScreen from './src/screens/HomeScreen';

const App: React.FC = () => {
  return (
    <>
      <StatusBar style="dark" />
      <HomeScreen />
    </>
  );
};

export default App;
