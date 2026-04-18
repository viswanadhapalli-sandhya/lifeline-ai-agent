// src/pages/HealthForm.jsx
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { postJSON } from "../services/api";

import { auth, db } from "../services/firebase";
import { addDoc, collection, serverTimestamp } from "firebase/firestore";

export default function HealthForm() {
  const navigate = useNavigate();

  const [form, setForm] = useState({
    age: "",
    gender: "",
    height: "",
    weight: "",
    sleep: 7,
    exercise: 30,
    activity: "",
    diet: "",
    stress: "",
    smoking: "",
    alcohol: "",
    medical: [],
  });

  const [loading, setLoading] = useState(false);

  const handleChange = (e) => setForm({ ...form, [e.target.name]: e.target.value });

  const calculateBMI = () => {
    if (!form.height || !form.weight) return "0.0";
    const h = Number(form.height) / 100;
    return (Number(form.weight) / (h * h)).toFixed(1);
  };

  const handleMedicalChange = (value) => {
    let updated = [...form.medical];

    if (value === "None") updated = ["None"];
    else {
      updated = updated.filter((v) => v !== "None");
      if (updated.includes(value)) updated = updated.filter((v) => v !== value);
      else updated.push(value);
    }

    setForm({ ...form, medical: updated });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (loading) return;

    try {
      setLoading(true);

      const user = auth.currentUser;
      if (!user) {
        alert("Please login first (Google login) then fill the form.");
        return;
      }

      const stressMap = {
        Low: 3,
        Medium: 6,
        High: 9,
      };

      const medicalString =
        form.medical.length > 0
          ? form.medical.join(", ")
          : "None";

      const payload = {
        age: Number(form.age),
        gender: form.gender,

        height: form.height ? Number(form.height) : null,
        weight: form.weight ? Number(form.weight) : null,

        sleep: Number(form.sleep),
        exercise: Number(form.exercise),
        stress: stressMap[form.stress],

        smoking: form.smoking === "Yes",
        alcohol: form.alcohol === "Yes" ? 1 : 0,

        medical: medicalString,

        activity: form.activity || null,
        diet: form.diet || null,
      };


      // 1) Save form to Firestore under the user
      await addDoc(collection(db, "users", user.uid, "healthRecords"), {
        ...payload,
        createdAt: serverTimestamp(),
      });

      console.log("Submitting form payload:", payload);

// 2) Call backend: risk
const risk = await postJSON("http://127.0.0.1:8000/predict", payload);
console.log("Risk response:", risk);

// 3) Call backend: AI analysis
const analysis = await postJSON("http://127.0.0.1:8000/analyze", payload);
console.log("Analysis response:", analysis);

// 4) Store everything
localStorage.setItem("riskResult", JSON.stringify(risk));
localStorage.setItem("analysisResult", JSON.stringify(analysis));
console.log("Saved to localStorage");

// 5) Go directly to agent coach
navigate("/coach");


    } catch (err) {
      console.error(err);
      alert(err?.message || "Submission failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-black via-zinc-900 to-green-950 flex items-center justify-center p-6">
      <form
        onSubmit={handleSubmit}
        className="bg-zinc-900/90 backdrop-blur-lg border border-green-600/30 text-white w-full max-w-4xl rounded-3xl shadow-2xl p-10 space-y-10"
      >
        <div className="text-center">
          <h1 className="text-3xl font-bold text-green-400">Lifeline AI</h1>
          <p className="text-gray-400">Prevent Disease Before Diagnosis</p>
        </div>

        <Section title="Personal Information">
          <Input label="Age" name="age" type="number" value={form.age} onChange={handleChange} />
          <Select
            label="Gender"
            name="gender"
            value={form.gender}
            onChange={handleChange}
            options={["Male", "Female", "Other"]}
          />
          <Input label="Height (cm)" name="height" type="number" value={form.height} onChange={handleChange} />
          <Input label="Weight (kg)" name="weight" type="number" value={form.weight} onChange={handleChange} />
        </Section>

        <Section title="Lifestyle Habits">
          <Slider label="Sleep Hours" name="sleep" value={form.sleep} max={12} onChange={handleChange} />
          <Slider label="Exercise (Minutes/Day)" name="exercise" value={form.exercise} max={180} onChange={handleChange} />

          <Select
            label="Activity Level"
            name="activity"
            value={form.activity}
            onChange={handleChange}
            options={["Low (Mostly Sitting)", "Moderate", "High (Daily Workouts)"]}
          />
          <Select
            label="Diet Quality"
            name="diet"
            value={form.diet}
            onChange={handleChange}
            options={["Poor (Junk Food)", "Average", "Healthy (Balanced Meals)"]}
          />
          <Select
            label="Stress Level"
            name="stress"
            value={form.stress}
            onChange={handleChange}
            options={["Low", "Medium", "High"]}
          />
        </Section>

        <Section title="Habits">
          <Select label="Smoking" name="smoking" value={form.smoking} onChange={handleChange} options={["No", "Yes"]} />
          <Select label="Alcohol" name="alcohol" value={form.alcohol} onChange={handleChange} options={["No", "Yes"]} />
        </Section>

        <div className="space-y-4">
          <h2 className="text-green-400 font-semibold">Medical Background</h2>
          <div className="grid grid-cols-2 gap-4 text-gray-300">
            {["Diabetes", "Hypertension (BP)", "Heart Disease", "Thyroid", "None"].map((item) => (
              <label key={item} className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.medical.includes(item)}
                  onChange={() => handleMedicalChange(item)}
                  className="accent-green-500 w-4 h-4"
                />
                {item}
              </label>
            ))}
          </div>
        </div>

        {form.height && form.weight && (
          <div className="text-center text-green-400 font-semibold text-lg">
            BMI: {calculateBMI()}
          </div>
        )}

        <button
          disabled={loading}
          className="w-full bg-green-500 hover:bg-green-600 text-black font-bold py-3 rounded-xl transition transform hover:scale-105 disabled:opacity-50"
        >
          {loading ? "Analyzing..." : "Risk Analysis"}
        </button>
      </form>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="space-y-4">
      <h2 className="text-green-400 font-semibold">{title}</h2>
      <div className="grid grid-cols-2 gap-4">{children}</div>
    </div>
  );
}

function Input({ label, ...props }) {
  return (
    <div className="flex flex-col">
      <label className="text-sm text-gray-400">{label}</label>
      <input
        required
        {...props}
        className="bg-zinc-800 border border-zinc-700 rounded-lg p-2 focus:ring-2 focus:ring-green-500 outline-none text-white"
      />
    </div>
  );
}

function Select({ label, options, ...props }) {
  return (
    <div className="flex flex-col">
      <label className="text-sm text-gray-400">{label}</label>
      <select
        required
        {...props}
        className="bg-zinc-800 border border-zinc-700 rounded-lg p-2 focus:ring-2 focus:ring-green-500 text-white"
      >
        <option value="">Select</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </div>
  );
}

function Slider({ label, name, value, onChange, max }) {
  return (
    <div className="flex flex-col col-span-2">
      <label className="text-sm text-gray-400">
        {label}: <span className="text-green-400">{value}</span>
      </label>
      <input type="range" name={name} value={value} min="0" max={max} onChange={onChange} className="accent-green-500" />
    </div>
  );
}
