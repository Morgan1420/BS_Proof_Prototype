import * as ImagePicker from "expo-image-picker";
import React from "react";
import { Image, Platform, Pressable, StyleSheet, Text, View } from "react-native";

const IMAGE_OPTIONS = {
  mediaTypes: ImagePicker.MediaTypeOptions.Images,
  quality: 0.8,
};

/**
 * Preview container + "select or capture" buttons for the label photo.
 *
 * onImageSelected receives the raw expo-image-picker asset object
 * (result.assets[0]) -- the parent owns which one is "current".
 */
export default function ImageUploadSection({ imageUri, onImageSelected, disabled }) {
  const pickFromLibrary = async () => {
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      alert("Permission to access photos is required.");
      return;
    }
    const result = await ImagePicker.launchImageLibraryAsync(IMAGE_OPTIONS);
    if (!result.canceled) onImageSelected(result.assets[0]);
  };

  const takePhoto = async () => {
    if (Platform.OS === "web") {
      alert("Camera capture isn't available on web -- choose a photo file instead.");
      return;
    }
    const permission = await ImagePicker.requestCameraPermissionsAsync();
    if (!permission.granted) {
      alert("Camera permission is required.");
      return;
    }
    const result = await ImagePicker.launchCameraAsync(IMAGE_OPTIONS);
    if (!result.canceled) onImageSelected(result.assets[0]);
  };

  return (
    <View style={styles.container}>
      <View style={styles.previewBox}>
        {imageUri ? (
          <Image source={{ uri: imageUri }} style={styles.preview} resizeMode="contain" />
        ) : (
          <Text style={styles.placeholderText}>No label selected yet</Text>
        )}
      </View>

      <View style={styles.buttonRow}>
        <Pressable
          style={[styles.pickButton, disabled && styles.disabled]}
          onPress={pickFromLibrary}
          disabled={disabled}
        >
          <Text style={styles.pickButtonText}>Choose Photo</Text>
        </Pressable>
        <Pressable
          style={[styles.pickButton, disabled && styles.disabled]}
          onPress={takePhoto}
          disabled={disabled}
        >
          <Text style={styles.pickButtonText}>Take Photo</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { width: "100%", marginBottom: 16 },
  previewBox: {
    height: 260,
    borderRadius: 12,
    borderWidth: 2,
    borderColor: "#d0d5dd",
    borderStyle: "dashed",
    backgroundColor: "#f9fafb",
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  preview: { width: "100%", height: "100%" },
  placeholderText: { color: "#667085" },
  buttonRow: { flexDirection: "row", gap: 12, marginTop: 12, justifyContent: "center" },
  pickButton: {
    paddingVertical: 10,
    paddingHorizontal: 16,
    borderRadius: 8,
    backgroundColor: "#eef2ff",
  },
  pickButtonText: { color: "#3730a3", fontWeight: "600" },
  disabled: { opacity: 0.5 },
});
