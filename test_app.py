from streamlit.testing.v1 import AppTest

at = AppTest.from_file("app.py", default_timeout=60).run()
assert not at.exception, at.exception
run = [b for b in at.button if b.label == "Run allocation"][0]
run.click().run()
assert not at.exception, at.exception
print("success:", [s.value for s in at.success])
print("metrics:", [(m.label, m.value) for m in at.metric])
print("warnings shown:", len(at.markdown))
