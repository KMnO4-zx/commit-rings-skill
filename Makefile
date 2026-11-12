.PHONY: test package pack

test:
	python3 -m unittest discover -s tests -v

package:
	python3 scripts/package_skill.py

pack: package
