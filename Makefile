.PHONY: install demo test validate cfr-smoke cfr-curve-smoke clean

install:
	@echo ">> Installing Cython<3 (eval7 build dep)"
	pip3 install "Cython<3"
	@echo ">> Installing eval7 with --no-build-isolation"
	pip3 install --no-build-isolation eval7==0.1.7
	@echo ">> Installing rest of requirements"
	pip3 install flask numpy scipy treys scikit-learn

demo:
	python3 demo.py

test:
	python3 -m pytest tests/ -q

validate:
	@if [ -z "$(BOT)" ]; then echo "usage: make validate BOT=bots/mybot/bot.py"; exit 1; fi
	python3 sandbox/validator.py $(BOT)

cfr-smoke:
	pixi run python bots/cfr/trainer/train.py --iterations 5 --samples 1 --equity-samples 4 --output bots/cfr/data/cfr_strategy.npz --log-interval 1
	rm -rf bots/cfr/dist
	mkdir -p bots/cfr/dist
	pixi run python -c "import zipfile; z=zipfile.ZipFile('bots/cfr/dist/cfr_bot.zip', 'w', zipfile.ZIP_DEFLATED); z.write('bots/cfr/bot.py', 'bot.py'); z.write('bots/cfr/data/cfr_strategy.npz', 'data/cfr_strategy.npz'); z.close()"
	pixi run python sandbox/validator.py bots/cfr/dist/cfr_bot.zip

cfr-curve-smoke:
	pixi run python bots/cfr/trainer/curve_experiment.py --max-iterations 10000 --step-iterations 10000 --samples 1 --equity-samples 4 --benchmark-iterations 1 --hands 50 --output-dir /tmp/cfr_curve_smoke

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
