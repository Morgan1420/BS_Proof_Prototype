import React from 'react';
import {
  View,
  Text,
  Image,
  Pressable,
  StyleSheet,
  Alert,
} from 'react-native';
import * as ImagePicker from 'expo-image-picker';

/**
 * Props for the ImageUploader component.
 */
export interface ImageUploaderProps {
  /** Currently selected image URI, or null if none selected yet. */
  imageUri: string | null;
  /** Called with the picked image URI (or null if the user cancelled). */
  onImageSelected: (uri: string | null) => void;
}

/**
 * Displays an "Upload Supplement Label" trigger and, once an image has been
 * picked, a preview of that image. Pure presentational + picker-invocation
 * component — it does not own the selected-image state itself.
 */
const ImageUploader: React.FC<ImageUploaderProps> = ({
  imageUri,
  onImageSelected,
}) => {
  const handlePickImage = async (): Promise<void> => {
    const permissionResult =
      await ImagePicker.requestMediaLibraryPermissionsAsync();

    if (!permissionResult.granted) {
      Alert.alert(
        'Permission required',
        'Please allow photo library access to upload a supplement label.'
      );
      return;
    }

    const result: ImagePicker.ImagePickerResult =
      await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ImagePicker.MediaTypeOptions.Images,
        allowsEditing: true,
        quality: 1,
      });

    if (result.canceled) {
      onImageSelected(null);
      return;
    }

    const pickedUri: string | undefined = result.assets?.[0]?.uri;
    onImageSelected(pickedUri ?? null);
  };

  return (
    <View style={styles.container}>
      <Pressable
        style={styles.uploadCard}
        onPress={handlePickImage}
        accessibilityRole="button"
        accessibilityLabel="Upload Supplement Label"
      >
        <Text style={styles.uploadCardText}>
          {imageUri ? 'Change Photo' : 'Upload Supplement Label'}
        </Text>
      </Pressable>

      {imageUri && (
        <Image
          source={{ uri: imageUri }}
          style={styles.previewImage}
          resizeMode="contain"
          accessibilityLabel="Selected supplement label preview"
        />
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    width: '100%',
    alignItems: 'center',
  },
  uploadCard: {
    width: 260,
    paddingVertical: 20,
    paddingHorizontal: 16,
    borderRadius: 12,
    borderWidth: 2,
    borderColor: '#4A90E2',
    borderStyle: 'dashed',
    backgroundColor: '#F5F9FF',
    alignItems: 'center',
    justifyContent: 'center',
  },
  uploadCardText: {
    fontSize: 16,
    fontWeight: '600',
    color: '#4A90E2',
    textAlign: 'center',
  },
  previewImage: {
    width: 260,
    height: 260,
    marginTop: 20,
    borderRadius: 12,
    backgroundColor: '#EEE',
  },
});

export default ImageUploader;
