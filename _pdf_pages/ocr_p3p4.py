import pymupdf
from rapidocr_onnxruntime import RapidOCR

PDF = r"产品资料——成像上位机及协议/产品资料——成像上位机及协议/4D成像雷达使用说明（上手必看）.pdf"
doc = pymupdf.open(PDF)
engine = RapidOCR()

with open("_pdf_pages/ocr_p3p4.txt", "w", encoding="utf-8") as f:
    for i in [2, 3]:
        doc[i].get_pixmap(dpi=300).save(f"_pdf_pages/hi_page{i+1}.png")
        f.write(f"===== PAGE {i+1} embedded =====\n")
        f.write(doc[i].get_text() + "\n")
        f.write(f"===== PAGE {i+1} OCR =====\n")
        result, _ = engine(f"_pdf_pages/hi_page{i+1}.png")
        if result:
            for line in result:
                f.write(line[1] + "\n")
        f.write("\n")
print("done")
