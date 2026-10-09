import 'dart:typed_data';

class PhotoUpload {
  const PhotoUpload({
    required this.bytes,
    required this.fileName,
    required this.mimeType,
    required this.previewPath,
  });

  final Uint8List bytes;
  final String fileName;
  final String mimeType;
  final String previewPath;

  bool get isEmpty => bytes.isEmpty;
}

/// Backend 가 받는 사진 한 장의 최대 크기 (backend 의 PHOTO_UPLOAD_MAX_BYTES 와 같다).
const maxPhotoUploadBytes = 20 * 1024 * 1024;

/// 사진을 고를 때 image_picker 에 넘길 압축 품질.
///
/// 웹에서는 품질을 지정하면 플러그인이 캔버스로 다시 그려 EXIF(촬영 좌표)가
/// 사라지므로 원본을 그대로 올린다. 큰 사진은 Backend 가 줄여서 인식 서비스로 보낸다.
/// Android·iOS 플러그인은 다시 인코딩한 파일에 EXIF 를 옮겨 준다.
int? photoPickImageQuality({required bool isWeb}) => isWeb ? null : 80;
