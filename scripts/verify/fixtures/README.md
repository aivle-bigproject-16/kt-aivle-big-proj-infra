# 판정 정확성 회귀 픽스처

`normal-ct.png`는 `C:\Users\rudtn\Downloads\빅프로젝트_데이터\데이터 전처리\battery_v41_output\CT\test\images\CT__CT_cell_pouch_101_x_022__d7515eca.jpg`를 바이트 단위로 그대로 복사한 CT 정상 셀 픽스처이다. 검증 하네스의 기본 파일명 계약이 `normal-ct.png`이므로 확장자만 그 이름을 따르며, 파일 내용의 인코딩은 원본과 동일한 JPEG이다.

이 전처리 이미지의 원본은 `C:\Users\rudtn\Downloads\빅프로젝트_데이터\데이터 전처리\103.배터리 불량 이미지 데이터\3.개방데이터\1.데이터\Training\01.원천데이터\TS_CT_Datasets_images_1\CT_cell_pouch_101_x_022.jpg`이다. `battery_v41_output\reports\manifest.csv`의 같은 sample_id 행은 이 원본과 전처리 이미지를 연결하며, `original_is_normal=True`, `is_normal_interpreted=True`, `has_damaged=False`, `has_pollution=False`, `has_porosity=False`, `original_defect_count=0`으로 기록한다.

대응 라벨은 `C:\Users\rudtn\Downloads\빅프로젝트_데이터\데이터 전처리\battery_v41_output\CT\test\labels_json\Training\02.라벨링데이터\TL_CT_Datasets_label\CT_cell_pouch_101_x_022.json`이다. 이 JSON은 `data_info.data_type`을 `ct`, `data_info.type`을 `cell`, `image_info.is_normal`을 `true`, `defects`를 `null`, `swelling.swelling`을 `false`로 명시하며, 대응하는 `labels_det\CT__CT_cell_pouch_101_x_022__d7515eca.txt`도 비어 있으므로 결함이 없는 정상 셀로 판단했다.
