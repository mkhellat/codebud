TEXI  := codebud.texi
BUILD := build

.PHONY: all html pdf info text clean

all: html pdf info

html:
	@mkdir -p $(BUILD)/html
	makeinfo --html --no-split -o $(BUILD)/html/codebud.html $(TEXI)

pdf:
	@mkdir -p $(BUILD)/pdf
	texi2pdf -o $(BUILD)/pdf/codebud.pdf -q --build-dir=$(BUILD)/pdf-tmp $(TEXI)

info:
	@mkdir -p $(BUILD)/info
	makeinfo -o $(BUILD)/info/codebud.info $(TEXI)

text:
	@mkdir -p $(BUILD)/text
	makeinfo --plaintext -o $(BUILD)/text/codebud.txt $(TEXI)

clean:
	rm -rf $(BUILD)
