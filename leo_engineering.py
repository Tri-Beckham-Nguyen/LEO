"""
LEO's advanced engineering tools.
All run locally — no cloud tokens except the tool call itself.
"""
import os
import json
import subprocess
from pathlib import Path


# --- CAD / 3D geometry ------------------------------------------------------

def cad(action, **kwargs):
    """Create, load, manipulate, and export 3D geometry using trimesh."""
    try:
        import trimesh
    except ImportError:
        return "trimesh is not installed. Run 'pip install trimesh scikit-image'."

    action = (action or "").lower().strip()
    source = kwargs.get("source", "")
    destination = kwargs.get("destination", "")

    # Helper to get a mesh from kwargs
    def _make_primitive():
        shape = kwargs.get("shape", "box").lower()
        dims = kwargs.get("dimensions")
        if isinstance(dims, str):
            dims = json.loads(dims)
        if not dims:
            return None, "Provide dimensions as JSON list, e.g. [1,2,3]."
        if shape == "box":
            return trimesh.creation.box(extents=dims), None
        elif shape == "sphere":
            if len(dims) < 1:
                return None, "Sphere needs radius."
            return trimesh.creation.icosphere(subdivisions=2, radius=dims[0]), None
        elif shape == "cylinder":
            if len(dims) < 2:
                return None, "Cylinder needs radius and height."
            return trimesh.creation.cylinder(radius=dims[0], height=dims[1]), None
        else:
            return None, f"Unknown shape '{shape}'."

    if action == "create":
        mesh, err = _make_primitive()
        if err:
            return err
        if not destination:
            destination = "leo_mesh.stl"
        mesh.export(destination)
        return f"Created {kwargs.get('shape','box')} mesh and exported to {destination}."

    elif action == "load":
        if not source:
            return "Provide source file path."
        mesh = trimesh.load(source)
        return f"Loaded {source}: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces."

    elif action == "info":
        if not source:
            return "Provide source file path."
        mesh = trimesh.load(source)
        info = {
            "vertices": len(mesh.vertices),
            "faces": len(mesh.faces),
            "bounds": mesh.bounds.tolist(),
            "volume": float(mesh.volume) if hasattr(mesh, "volume") else None,
            "area": float(mesh.area) if hasattr(mesh, "area") else None,
        }
        return json.dumps(info, indent=2)

    elif action in ("boolean_union", "boolean_difference", "boolean_intersection"):
        if not source:
            return "Provide first mesh path (source)."
        other = kwargs.get("other")
        if not other:
            return "Provide second mesh path (other)."
        mesh_a = trimesh.load(source)
        mesh_b = trimesh.load(other)
        if action == "boolean_union":
            result = trimesh.boolean.union([mesh_a, mesh_b])
        elif action == "boolean_difference":
            result = trimesh.boolean.difference([mesh_a, mesh_b])
        else:
            result = trimesh.boolean.intersection([mesh_a, mesh_b])
        if destination:
            result.export(destination)
            return f"Boolean {action} exported to {destination}."
        return f"Boolean {action} complete. (No destination provided; result not saved.)"

    elif action == "export":
        if not source:
            return "Provide source file path."
        if not destination:
            return "Provide destination file path."
        mesh = trimesh.load(source)
        mesh.export(destination)
        return f"Exported {source} to {destination}."

    return "Unknown CAD action. Use create, load, info, boolean_union, boolean_difference, boolean_intersection, export."


# --- Numerical analysis ------------------------------------------------------

def analysis(action, data=None, **kwargs):
    """Numerical analysis using numpy/scipy. data should be a JSON array."""
    try:
        import numpy as np
        from scipy import fft, stats, integrate, linalg, optimize, interpolate
    except ImportError:
        return "numpy/scipy not installed. Run 'pip install numpy scipy'."

    action = (action or "").lower().strip()

    # Parse data if provided
    arr = None
    if data:
        if isinstance(data, str):
            try:
                arr = np.array(json.loads(data), dtype=float)
            except Exception:
                try:
                    arr = np.loadtxt(data, delimiter=',')
                except Exception:
                    return "Could not parse data. Provide a JSON array or CSV file path."
        elif isinstance(data, list):
            arr = np.array(data, dtype=float)
        else:
            return "Data must be a JSON array or file path."

    if action == "fft":
        if arr is None:
            return "Provide data for FFT."
        if arr.ndim == 1:
            result = np.abs(fft.fft(arr))
            freqs = fft.fftfreq(len(arr))
            # Return top 10 frequencies by magnitude
            idx = np.argsort(result)[::-1][:10]
            top = [(float(freqs[i]), float(result[i])) for i in idx if result[i] > 0]
            return f"FFT complete. Top frequencies (Hz, magnitude): {top}"
        else:
            return "FFT only supports 1D data."

    elif action == "stats":
        if arr is None:
            return "Provide data for statistics."
        if arr.ndim == 1:
            return json.dumps({
                "mean": float(np.mean(arr)),
                "std": float(np.std(arr)),
                "min": float(np.min(arr)),
                "max": float(np.max(arr)),
                "median": float(np.median(arr)),
            }, indent=2)
        else:
            return "Stats only supports 1D data."

    elif action == "polyfit":
        if arr is None:
            return "Provide data as [[x1,y1],[x2,y2],...]."
        if arr.ndim != 2 or arr.shape[1] != 2:
            return "Data must be Nx2 array of x,y pairs."
        degree = int(kwargs.get("degree", 2))
        coeffs = np.polyfit(arr[:, 0], arr[:, 1], degree)
        return f"Polynomial coefficients (highest degree first): {coeffs.tolist()}"

    elif action == "solve_linear":
        if arr is None:
            return "Provide data as [A, b] where A is matrix and b is vector."
        if not isinstance(data, str):
            return "Provide JSON string: [[A_matrix], [b_vector]]."
        parsed = json.loads(data)
        if len(parsed) != 2:
            return "Provide [A, b]."
        A = np.array(parsed[0], dtype=float)
        b = np.array(parsed[1], dtype=float)
        try:
            x = linalg.solve(A, b)
            return f"Solution: {x.tolist()}"
        except Exception as e:
            return f"Solve failed: {e}"

    elif action == "integrate":
        if arr is None:
            return "Provide data as [[x,y],...]."
        if arr.ndim != 2 or arr.shape[1] != 2:
            return "Data must be Nx2 array of x,y pairs."
        x = arr[:, 0]
        y = arr[:, 1]
        result = integrate.trapz(y, x)
        return f"Trapezoidal integral: {result}"

    elif action == "interpolate":
        if arr is None:
            return "Provide data as [[x,y],...]."
        if arr.ndim != 2 or arr.shape[1] != 2:
            return "Data must be Nx2 array of x,y pairs."
        x = arr[:, 0]
        y = arr[:, 1]
        f = interpolate.interp1d(x, y, kind=kwargs.get("kind", "cubic"), fill_value="extrapolate")
        xi = float(kwargs.get("x", 0))
        return f"Interpolated at x={xi}: {f(xi)}"

    elif action == "optimize":
        # Minimize a simple function passed as a string? We'll do least squares fit.
        return "Optimization not implemented in this tool; use run_code with scipy.optimize."

    return "Unknown analysis action. Use fft, stats, polyfit, solve_linear, integrate, interpolate."


# --- Engineering simulation formulas -----------------------------------------

def simulate(action, **kwargs):
    """Simple engineering formula simulations. No heavy FEA/CFD."""
    action = (action or "").lower().strip()

    if action == "beam_bending":
        # Simply supported beam with central point load or UDL
        length = float(kwargs.get("length", 1.0))       # m
        load = float(kwargs.get("load", 1000.0))        # N (point load) or N/m (UDL)
        E = float(kwargs.get("E", 200e9))               # Pa
        I = float(kwargs.get("I", 1e-6))                # m^4
        load_type = kwargs.get("load_type", "point").lower()
        if load_type == "point":
            max_deflection = (load * length**3) / (48 * E * I)
            max_bending_moment = load * length / 4
        else:  # UDL
            max_deflection = (5 * load * length**4) / (384 * E * I)
            max_bending_moment = load * length**2 / 8
        max_stress = max_bending_moment * (float(kwargs.get("c", 0.05))) / I  # c = distance from neutral axis
        return (f"Beam bending (simply supported, {load_type} load):\n"
                f"Max deflection: {max_deflection:.6f} m\n"
                f"Max bending moment: {max_bending_moment:.2f} N·m\n"
                f"Max stress: {max_stress/1e6:.2f} MPa")

    elif action == "heat_conduction":
        # 1D steady-state conduction through a composite wall
        thickness = float(kwargs.get("thickness", 0.1))   # m
        area = float(kwargs.get("area", 1.0))             # m^2
        k = float(kwargs.get("k", 50.0))                  # W/(m·K)
        T1 = float(kwargs.get("T1", 100.0))               # °C
        T2 = float(kwargs.get("T2", 25.0))                # °C
        R = thickness / (k * area)
        Q = (T1 - T2) / R
        return (f"Heat conduction (1D, steady):\n"
                f"Thermal resistance: {R:.6f} K/W\n"
                f"Heat transfer rate: {Q:.2f} W")

    elif action == "pipe_pressure_drop":
        # Darcy-Weisbach pressure drop in a pipe
        length = float(kwargs.get("length", 10.0))        # m
        diameter = float(kwargs.get("diameter", 0.05))    # m
        velocity = float(kwargs.get("velocity", 2.0))     # m/s
        rho = float(kwargs.get("density", 1000.0))        # kg/m^3
        f = float(kwargs.get("friction_factor", 0.02))    # dimensionless
        delta_p = f * (length / diameter) * (0.5 * rho * velocity**2)
        return (f"Pipe pressure drop (Darcy-Weisbach):\n"
                f"ΔP: {delta_p:.2f} Pa ({delta_p/1000:.4f} kPa)")

    elif action == "thermal_expansion":
        # Linear thermal expansion
        original_length = float(kwargs.get("length", 1.0))    # m
        alpha = float(kwargs.get("alpha", 12e-6))             # 1/°C
        delta_T = float(kwargs.get("delta_T", 50.0))          # °C
        delta_L = original_length * alpha * delta_T
        return (f"Thermal expansion:\n"
                f"ΔL: {delta_L*1000:.4f} mm")

    return "Unknown simulation action. Use beam_bending, heat_conduction, pipe_pressure_drop, thermal_expansion."