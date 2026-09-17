import os
import pymupdf
from rapidocr_onnxruntime import RapidOCR

PDF = r"产品资料——成像上位机及协议/产品资料——成像上位机及协议/4D成像雷达使用说明（上手必看）.pdf"
OUT = "_pdf_pages"

doc = pymupdf.open(PDF)
engine = RapidOCR()

for i in range(len(doc)):
    png = os.path.join(OUT, f"page{i+1}.png")
    doc[i].get_pixmap(dpi=200).save(png)
    txt = doc[i].get_text().strip()
    print(f"===== PAGE {i+1} (embedded text) =====")
    print(txt if txt else "(no embedded text)")
    result, _ = engine(png)
    if result:
        print(f"----- PAGE {i+1} OCR -----")
        for line in result:
            print(line[1])
    print()
