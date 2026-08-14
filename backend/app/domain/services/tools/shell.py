import json
import shlex
from typing import Any, ClassVar, Optional
from app.domain.external.sandbox import Sandbox
from app.domain.services.tools.base import BaseToolkit
from langchain.tools import tool
from app.domain.models.tool_result import ToolResult

class ShellToolkit(BaseToolkit):
    """Shell tool class, providing Shell interaction related functions"""

    name: str = "shell"
    
    PLUGIN_MANAGED_TOOL_NAMES: ClassVar[set[str]] = {
        "scientific_inspect",
        "scientific_statistics",
        "scientific_aggregate",
        "scientific_subset",
        "scientific_convert_netcdf_to_geotiff",
        "scientific_transform_raster",
        "scientific_raster_index",
        "scientific_terrain",
        "scientific_visualize",
        "scientific_netcdf_visualize",
        "scientific_point_timeseries",
        "scientific_region_timeseries",
        "scientific_region_statistics",
        "scientific_last_dimension_profile",
    }

    def __init__(self, sandbox: Sandbox, *, include_plugin_managed_tools: bool = True):
        """Initialize Shell tool class
        
        Args:
            sandbox: Sandbox service
        """
        super().__init__()
        self.sandbox = sandbox
        self.include_plugin_managed_tools = include_plugin_managed_tools

    def get_tools(self):
        tools = super().get_tools()
        if self.include_plugin_managed_tools:
            return tools
        return [tool for tool in tools if tool.name not in self.PLUGIN_MANAGED_TOOL_NAMES]

    def get_tool(self, tool_name: str):
        if not self.include_plugin_managed_tools and tool_name in self.PLUGIN_MANAGED_TOOL_NAMES:
            return None
        return super().get_tool(tool_name)
        
    @tool(parse_docstring=True)
    async def shell_exec(
        self,
        id: str,
        exec_dir: str,
        command: str
    ) -> ToolResult:
        """Start a command in a specified shell session. Use for interactive or long-running processes; prefer shell_run for bounded non-interactive commands.
        
        Args:
            id: Unique identifier of the target shell session
            exec_dir: Working directory for command execution (must use absolute path)
            command: Shell command to execute
        """
        return await self.sandbox.exec_command(id, exec_dir, command)

    @tool(parse_docstring=True)
    async def shell_run(
        self,
        id: str,
        exec_dir: str,
        command: str,
        timeout_seconds: int = 30,
    ) -> ToolResult:
        """Run a bounded non-interactive command and wait once for its result. Prefer this for inspection, extraction, scripts, and data processing that should finish promptly.

        Args:
            id: Unique identifier of the target shell session
            exec_dir: Working directory for command execution (must use absolute path)
            command: Shell command to execute
            timeout_seconds: Maximum bounded wait in seconds, clamped to 1-120
        """
        return await self._run_bounded_command(
            id=id,
            exec_dir=exec_dir,
            command=command,
            timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def dataset_unpack(
        self,
        id: str,
        archive_path: str,
        output_dir: str,
        timeout_seconds: int = 120,
        source_root: Optional[str] = None,
    ) -> ToolResult:
        """Safely extract a ZIP, RAR, or 7z dataset, including nested archives, in one bounded call. Prefer this over manually chaining archive commands. It rejects traversal, links, encrypted members, and excessive expansion, and returns a final-file manifest.

        Args:
            id: Unique identifier of the target shell session
            archive_path: Absolute path to the source archive inside the sandbox
            output_dir: Absolute path to a new output directory under /home/ubuntu/output
            timeout_seconds: Maximum bounded wait in seconds, clamped to 1-120
            source_root: Optional trusted dataset root; the resolved archive must remain below it
        """
        source_root_option = (
            f"--source-root {shlex.quote(source_root)} " if source_root else ""
        )
        command = (
            f"ai-dataseek-unpack {shlex.quote(archive_path)} "
            f"--output {shlex.quote(output_dir)} "
            f"{source_root_option}"
            f"--timeout-seconds {max(1, min(timeout_seconds, 120))}"
        )
        return await self._run_bounded_command(
            id=id,
            exec_dir="/home/ubuntu",
            command=command,
            timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def dataset_quicklook(
        self,
        id: str,
        input_path: str,
        output_dir: str,
        max_plots: int = 4,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Create a bounded model-free profile, compact evidence, and 1-4 useful PNG charts for one selected CSV/TSV, Excel, GeoTIFF, directory, or ZIP/RAR/7z input. Archives, including nested archives, are extracted safely. This returns evidence and artifacts; use it only when its scope directly matches the user's request, then decide whether the evidence fully answers that request. Use a new output directory under /home/ubuntu/output.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute dataset file, archive, or directory path inside the sandbox
            output_dir: Absolute new output directory under /home/ubuntu/output
            max_plots: Maximum number of PNG charts, clamped to 1-4
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        bounded_plots = max(1, min(max_plots, 4))
        bounded_timeout = max(5, min(timeout_seconds, 120))
        command = (
            f"ai-dataseek-quicklook {shlex.quote(input_path)} "
            f"--output {shlex.quote(output_dir)} "
            f"--max-plots {bounded_plots} "
            f"--timeout-seconds {bounded_timeout}"
        )
        return await self._run_bounded_command(
            id=id,
            exec_dir="/home/ubuntu",
            command=command,
            timeout_seconds=bounded_timeout,
        )

    @tool(parse_docstring=True)
    async def scientific_inspect(
        self,
        id: str,
        input_path: str,
        timeout_seconds: int = 30,
    ) -> ToolResult:
        """Inspect one NetCDF or GeoTIFF file with deterministic format-aware logic. Returns variables, dimensions, coordinate roles, units, missing-value metadata, CRS, transform, bounds, and ambiguity candidates without loading the full data cube.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF or GeoTIFF path inside the sandbox
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id,
            operation="inspect",
            input_path=input_path,
            timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def scientific_statistics(
        self,
        id: str,
        input_path: str,
        variable: Optional[str] = None,
        band: int = 1,
        dimension_indices: Optional[dict[str, int]] = None,
        timeout_seconds: int = 60,
    ) -> ToolResult:
        """Compute bounded, mask-aware statistics for one NetCDF variable or GeoTIFF band. NetCDF is CF-decoded with scale and missing values applied. If a NetCDF contains multiple candidate variables, inspect it and pass an explicit variable instead of guessing.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF or GeoTIFF path inside the sandbox
            variable: NetCDF data variable name; optional only when exactly one candidate exists
            band: One-based GeoTIFF band index
            dimension_indices: Optional NetCDF dimension-to-integer-index selection
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id,
            operation="statistics",
            input_path=input_path,
            variable=variable,
            band=band,
            dimension_indices=dimension_indices,
            timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def scientific_aggregate(
        self,
        id: str,
        input_path: str,
        dimension: str,
        method: str,
        variable: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        output_path: Optional[str] = None,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Apply an explicit labelled time or dimension reduction to one NetCDF variable. Supports mean, sum, min, max, and median; never guesses the variable or dimension and records the selected range and mask/scale provenance.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            dimension: Existing NetCDF dimension to reduce, such as time
            method: Reduction method: mean, sum, min, max, or median
            variable: NetCDF data variable name; required when multiple numeric variables exist
            start: Optional inclusive coordinate-range start for the selected dimension
            end: Optional inclusive coordinate-range end for the selected dimension
            output_path: Optional NetCDF path below /home/ubuntu/output; use it for non-scalar results
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        bounded_timeout = max(5, min(timeout_seconds, 120))
        parts = [
            "ai-dataseek-scientific", "aggregate", shlex.quote(input_path),
            "--dimension", shlex.quote(dimension), "--method", shlex.quote(method),
        ]
        if variable:
            parts.extend(["--variable", shlex.quote(variable)])
        if start:
            parts.extend(["--start", shlex.quote(start)])
        if end:
            parts.extend(["--end", shlex.quote(end)])
        if output_path:
            parts.extend(["--output", shlex.quote(output_path)])
        return await self._run_bounded_command(
            id=id,
            exec_dir="/home/ubuntu",
            command=" ".join(parts),
            timeout_seconds=bounded_timeout,
        )

    @tool(parse_docstring=True)
    async def scientific_subset(
        self,
        id: str,
        input_path: str,
        output_path: str,
        variable: Optional[str] = None,
        bbox: Optional[list[float]] = None,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        dimension_indices: Optional[dict[str, int]] = None,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Create a CF-aware NetCDF subset by an explicit longitude/latitude bounding box, time range, and/or dimension indices. It supports ascending or descending coordinates and records the selection; it does not silently handle a dateline-crossing area.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            output_path: New NetCDF path below /home/ubuntu/output
            variable: NetCDF data variable name; required when multiple numeric variables exist
            bbox: Optional [west, south, east, north] in the dataset longitude/latitude coordinate system
            time_start: Optional inclusive time-coordinate start
            time_end: Optional inclusive time-coordinate end
            dimension_indices: Optional NetCDF dimension-to-integer-index selection
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id, operation="subset", input_path=input_path, output_path=output_path,
            variable=variable, dimension_indices=dimension_indices, timeout_seconds=timeout_seconds,
            extra_args={"--bbox": bbox, "--time-start": time_start, "--time-end": time_end},
        )

    @tool(parse_docstring=True)
    async def scientific_convert_netcdf_to_geotiff(
        self,
        id: str,
        input_path: str,
        output_path: str,
        variable: Optional[str] = None,
        dimension_indices: Optional[dict[str, int]] = None,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Export a two-dimensional, regularly spaced latitude/longitude NetCDF selection to GeoTIFF. It validates the grid and writes EPSG:4326 only when that interpretation is supported by coordinate metadata.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            output_path: New GeoTIFF path below /home/ubuntu/output
            variable: NetCDF data variable name; required when multiple numeric variables exist
            dimension_indices: Required selections for non-spatial NetCDF dimensions when applicable
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id, operation="convert", input_path=input_path, output_path=output_path,
            variable=variable, dimension_indices=dimension_indices, timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def scientific_transform_raster(
        self,
        id: str,
        input_path: str,
        output_path: str,
        target_crs: Optional[str] = None,
        resolution: Optional[float] = None,
        bbox: Optional[list[float]] = None,
        resampling: str = "nearest",
        timeout_seconds: int = 120,
    ) -> ToolResult:
        """Clip, reproject, and/or resample a GeoTIFF in one deterministic operation. The bbox is interpreted in the source CRS; resampling is explicitly nearest, bilinear, cubic, or average.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute GeoTIFF path inside the sandbox
            output_path: New GeoTIFF path below /home/ubuntu/output
            target_crs: Optional target CRS such as EPSG:4326 or EPSG:3857
            resolution: Optional positive target resolution in target-CRS units
            bbox: Optional [left, bottom, right, top] in the source CRS
            resampling: nearest, bilinear, cubic, or average
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id, operation="transform", input_path=input_path, output_path=output_path,
            timeout_seconds=timeout_seconds,
            extra_args={"--target-crs": target_crs, "--resolution": resolution, "--bbox": bbox, "--resampling": resampling},
        )

    @tool(parse_docstring=True)
    async def scientific_raster_index(
        self,
        id: str,
        input_path: str,
        output_path: str,
        index_name: str,
        bands: dict[str, int],
        timeout_seconds: int = 120,
    ) -> ToolResult:
        """Calculate NDVI, EVI, NDWI, or NBR from explicitly mapped one-based GeoTIFF bands. Never infer sensor-specific band meanings from band positions; inspect metadata or ask the user when the mapping is unknown.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute multiband GeoTIFF path inside the sandbox
            output_path: New single-band GeoTIFF path below /home/ubuntu/output
            index_name: ndvi, evi, ndwi, or nbr
            bands: Exact semantic band mapping, such as {"nir": 4, "red": 3}
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id, operation="raster-index", input_path=input_path, output_path=output_path,
            timeout_seconds=timeout_seconds,
            extra_args={"--index": index_name, "--bands": bands},
        )

    @tool(parse_docstring=True)
    async def scientific_terrain(
        self,
        id: str,
        input_path: str,
        output_path: str,
        operation: str,
        band: int = 1,
        timeout_seconds: int = 120,
    ) -> ToolResult:
        """Calculate slope or aspect from one projected GeoTIFF DEM band. Geographic-degree rasters are rejected because terrain derivatives require projected linear cell units.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute projected GeoTIFF DEM path inside the sandbox
            output_path: New single-band GeoTIFF path below /home/ubuntu/output
            operation: slope or aspect
            band: One-based DEM band index
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id, operation="terrain", input_path=input_path, output_path=output_path,
            timeout_seconds=timeout_seconds,
            extra_args={"--terrain-operation": operation, "--band": max(1, band)},
        )

    @tool(parse_docstring=True)
    async def scientific_visualize(
        self,
        id: str,
        input_path: str,
        output_path: str,
        variable: Optional[str] = None,
        band: int = 1,
        dimension_indices: Optional[dict[str, int]] = None,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Create one coordinate-aware PNG from a two-dimensional NetCDF selection or a GeoTIFF band. Provide dimension indices for non-spatial NetCDF dimensions. The operator applies declared masks and geospatial coordinates and returns a verifiable artifact record.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF or GeoTIFF path inside the sandbox
            output_path: Absolute PNG path below /home/ubuntu/output
            variable: NetCDF data variable name; optional only when exactly one candidate exists
            band: One-based GeoTIFF band index
            dimension_indices: Optional NetCDF dimension-to-integer-index selection
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id,
            operation="visualize",
            input_path=input_path,
            output_path=output_path,
            variable=variable,
            band=band,
            dimension_indices=dimension_indices,
            timeout_seconds=timeout_seconds,
        )

    @tool(parse_docstring=True)
    async def scientific_netcdf_visualize(
        self,
        id: str,
        input_path: str,
        output_dir: str,
        variable: Optional[str] = None,
        max_plots: int = 4,
        dimension_indices: Optional[dict[str, int]] = None,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Create a representative coordinate-aware PNG bundle from one NetCDF file in one operation. Use this for a general request to plot or visualize a named NetCDF file. It identifies an unambiguous numeric variable, latitude/longitude axes, and one remaining time-like dimension; generates up to four representative slices including a temporal mean; and returns verified image artifacts. Use scientific_visualize instead when the user specifies one exact slice or custom selection.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            output_dir: New output directory below /home/ubuntu/output
            variable: Optional explicit NetCDF variable; omit only when one numeric candidate exists
            max_plots: Maximum representative PNG count, clamped to 1-4
            dimension_indices: Indices for additional non-spatial dimensions that must be fixed
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_scientific_command(
            id=id,
            operation="visualize-bundle",
            input_path=input_path,
            output_path=output_dir,
            variable=variable,
            dimension_indices=dimension_indices,
            timeout_seconds=timeout_seconds,
            extra_args={"--max-plots": max(1, min(max_plots, 4))},
        )

    @tool(parse_docstring=True)
    async def scientific_point_timeseries(
        self, id: str, input_path: str, variable: Optional[str] = None,
        latitude: Optional[float] = None, longitude: Optional[float] = None,
        latitude_index: Optional[int] = None, longitude_index: Optional[int] = None,
        time_coordinate: Optional[str] = None,
        latitude_coordinate: Optional[str] = None,
        longitude_coordinate: Optional[str] = None,
        dimension_indices: Optional[dict[str, int]] = None,
        max_points: int = 480, timeout_seconds: int = 90,
    ) -> ToolResult:
        """Extract a deterministic point time series from a NetCDF variable. Select a grid cell by latitude/longitude values or explicit indices. Inspect first and pass coordinate names when CF roles are ambiguous.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            variable: NetCDF data variable name; required when multiple numeric variables exist
            latitude: Latitude value; the nearest grid coordinate is selected
            longitude: Longitude value; the nearest grid coordinate is selected
            latitude_index: Optional explicit latitude index, taking precedence over latitude
            longitude_index: Optional explicit longitude index, taking precedence over longitude
            time_coordinate: Optional one-dimensional time coordinate name
            latitude_coordinate: Optional one-dimensional latitude coordinate name
            longitude_coordinate: Optional one-dimensional longitude coordinate name
            dimension_indices: Explicit selections for all extra dimensions
            max_points: Maximum returned points, clamped to 2-2000
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_recipe_command(
            id=id, operation="point-timeseries", input_path=input_path,
            variable=variable, dimension_indices=dimension_indices,
            max_points=max_points, timeout_seconds=timeout_seconds,
            extra_args={
                "--latitude": latitude, "--longitude": longitude,
                "--latitude-index": latitude_index, "--longitude-index": longitude_index,
                "--time-coordinate": time_coordinate,
                "--latitude-coordinate": latitude_coordinate,
                "--longitude-coordinate": longitude_coordinate,
            },
        )

    @tool(parse_docstring=True)
    async def scientific_region_timeseries(
        self, id: str, input_path: str, method: str,
        variable: Optional[str] = None, bbox: Optional[list[float]] = None,
        polygon: Optional[list[list[float]]] = None,
        time_coordinate: Optional[str] = None,
        latitude_coordinate: Optional[str] = None,
        longitude_coordinate: Optional[str] = None,
        dimension_indices: Optional[dict[str, int]] = None,
        max_points: int = 480, timeout_seconds: int = 90,
    ) -> ToolResult:
        """Compute a region or full-grid NetCDF time series using an explicit spatial mean, maximum, minimum, or median at every time step.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            method: Spatial reducer: mean, max, min, or median
            variable: NetCDF data variable name; required when multiple numeric variables exist
            bbox: Optional [west, south, east, north] in EPSG:4326 coordinates
            polygon: Optional polygon vertices as [[longitude, latitude], ...], preferred over bbox
            time_coordinate: Optional one-dimensional time coordinate name
            latitude_coordinate: Optional one-dimensional latitude coordinate name
            longitude_coordinate: Optional one-dimensional longitude coordinate name
            dimension_indices: Explicit selections for all extra dimensions
            max_points: Maximum returned points, clamped to 2-2000
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_recipe_command(
            id=id, operation="region-timeseries", input_path=input_path,
            variable=variable, dimension_indices=dimension_indices,
            max_points=max_points, timeout_seconds=timeout_seconds,
            extra_args={
                "--method": method, "--bbox": bbox, "--polygon": polygon,
                "--time-coordinate": time_coordinate,
                "--latitude-coordinate": latitude_coordinate,
                "--longitude-coordinate": longitude_coordinate,
            },
        )

    @tool(parse_docstring=True)
    async def scientific_region_statistics(
        self, id: str, input_path: str, method: str,
        variable: Optional[str] = None, bbox: Optional[list[float]] = None,
        polygon: Optional[list[list[float]]] = None,
        latitude_coordinate: Optional[str] = None,
        longitude_coordinate: Optional[str] = None,
        dimension_indices: Optional[dict[str, int]] = None,
        timeout_seconds: int = 90,
    ) -> ToolResult:
        """Compute a maximum, minimum, or median for one explicitly selected two-dimensional NetCDF field. Maximum and minimum include the coordinate location.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            method: Statistic: max, min, or median
            variable: NetCDF data variable name; required when multiple numeric variables exist
            bbox: Optional [west, south, east, north] in EPSG:4326 coordinates
            polygon: Optional polygon vertices as [[longitude, latitude], ...], preferred over bbox
            latitude_coordinate: Optional one-dimensional latitude coordinate name
            longitude_coordinate: Optional one-dimensional longitude coordinate name
            dimension_indices: Explicit selections for all non-spatial dimensions
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_recipe_command(
            id=id, operation="region-statistics", input_path=input_path,
            variable=variable, dimension_indices=dimension_indices,
            timeout_seconds=timeout_seconds,
            extra_args={
                "--method": method, "--bbox": bbox, "--polygon": polygon,
                "--latitude-coordinate": latitude_coordinate,
                "--longitude-coordinate": longitude_coordinate,
            },
        )

    @tool(parse_docstring=True)
    async def scientific_last_dimension_profile(
        self, id: str, input_path: str, dimension: str,
        variable: Optional[str] = None,
        dimension_indices: Optional[dict[str, int]] = None,
        max_points: int = 480, timeout_seconds: int = 90,
    ) -> ToolResult:
        """Create a profile along one explicit NetCDF dimension by averaging all remaining unselected dimensions.

        Args:
            id: Unique identifier of the target shell session
            input_path: Absolute NetCDF path inside the sandbox
            dimension: Existing dimension to preserve as the profile axis
            variable: NetCDF data variable name; required when multiple numeric variables exist
            dimension_indices: Optional dimensions to select before averaging the rest
            max_points: Maximum returned profile points, clamped to 2-2000
            timeout_seconds: Maximum bounded wait in seconds, clamped to 5-120
        """
        return await self._run_recipe_command(
            id=id, operation="last-dimension-profile", input_path=input_path,
            variable=variable, dimension_indices=dimension_indices,
            max_points=max_points, timeout_seconds=timeout_seconds,
            extra_args={"--dimension": dimension},
        )

    async def _run_recipe_command(
        self, *, id: str, operation: str, input_path: str,
        timeout_seconds: int, variable: Optional[str] = None,
        dimension_indices: Optional[dict[str, int]] = None,
        max_points: Optional[int] = None,
        extra_args: Optional[dict[str, Any]] = None,
    ) -> ToolResult:
        bounded_timeout = max(5, min(timeout_seconds, 120))
        parts = ["ai-dataseek-scientific-recipe", operation, shlex.quote(input_path)]
        if variable:
            parts.extend(["--variable", shlex.quote(variable)])
        parts.extend(["--dimension-indices", shlex.quote(json.dumps(dimension_indices or {}, ensure_ascii=True))])
        if max_points is not None:
            parts.extend(["--max-points", str(max(2, min(max_points, 2000)))])
        for flag, value in (extra_args or {}).items():
            if value is None:
                continue
            rendered = json.dumps(value, ensure_ascii=True) if isinstance(value, (dict, list)) else str(value)
            parts.extend([flag, shlex.quote(rendered)])
        return await self._run_bounded_command(
            id=id, exec_dir="/home/ubuntu", command=" ".join(parts),
            timeout_seconds=bounded_timeout,
        )

    async def _run_scientific_command(
        self,
        *,
        id: str,
        operation: str,
        input_path: str,
        timeout_seconds: int,
        output_path: Optional[str] = None,
        variable: Optional[str] = None,
        band: int = 1,
        dimension_indices: Optional[dict[str, int]] = None,
        extra_args: Optional[dict[str, Any]] = None,
    ) -> ToolResult:
        bounded_timeout = max(5, min(timeout_seconds, 120))
        parts = [
            "ai-dataseek-scientific",
            operation,
            shlex.quote(input_path),
        ]
        if operation in {"statistics", "subset", "convert", "visualize", "visualize-bundle"}:
            if variable:
                parts.extend(["--variable", shlex.quote(variable)])
            parts.extend([
                "--dimension-indices",
                shlex.quote(json.dumps(dimension_indices or {}, ensure_ascii=True)),
            ])
        if operation in {"statistics", "visualize"}:
            parts.extend(["--band", str(max(1, band))])
        if output_path:
            parts.extend(["--output", shlex.quote(output_path)])
        for flag, value in (extra_args or {}).items():
            if value is None:
                continue
            rendered = json.dumps(value, ensure_ascii=True) if isinstance(value, (dict, list)) else str(value)
            parts.extend([flag, shlex.quote(rendered)])
        return await self._run_bounded_command(
            id=id,
            exec_dir="/home/ubuntu",
            command=" ".join(parts),
            timeout_seconds=bounded_timeout,
        )

    async def _run_bounded_command(
        self,
        *,
        id: str,
        exec_dir: str,
        command: str,
        timeout_seconds: int,
    ) -> ToolResult:
        timeout_seconds = max(1, min(timeout_seconds, 120))
        exec_result = await self.sandbox.exec_command(id, exec_dir, command)
        exec_data = self._result_data(exec_result)
        if exec_data.get("status") != "running":
            return exec_result

        wait_result = await self.sandbox.wait_for_process(id, timeout_seconds)
        wait_data = self._result_data(wait_result)
        if wait_data.get("status") != "completed":
            return ToolResult(
                success=wait_result.success,
                message=f"Command is still running after {timeout_seconds} seconds",
                data={
                    "session_id": id,
                    "command": command,
                    "status": "running",
                    "returncode": None,
                },
            )

        view_result = await self.sandbox.view_shell(id)
        view_data = self._result_data(view_result)
        returncode = wait_data.get("returncode")
        succeeded = returncode == 0 and view_result.success
        return ToolResult(
            success=succeeded,
            message=(
                "Command completed successfully"
                if succeeded
                else f"Command failed with return code: {returncode}"
            ),
            data={
                "session_id": id,
                "command": command,
                "status": "completed",
                "returncode": returncode,
                "output": view_data.get("output", ""),
            },
        )

    @staticmethod
    def _result_data(result: ToolResult) -> dict[str, Any]:
        return result.data if isinstance(result.data, dict) else {}
    
    @tool(parse_docstring=True)
    async def shell_view(self, id: str) -> ToolResult:
        """View the content of a specified shell session. Use for checking command execution results or monitoring output.
        
        Args:
            id: Unique identifier of the target shell session
        """
        return await self.sandbox.view_shell(id)
    
    @tool(parse_docstring=True)
    async def shell_wait(
        self,
        id: str,
        seconds: Optional[int] = None
    ) -> ToolResult:
        """Wait for the running process in a specified shell session to return. Use after running commands that require longer runtime.
        
        Args:
            id: Unique identifier of the target shell session
            seconds: Wait duration in seconds
        """
        return await self.sandbox.wait_for_process(id, seconds)
    
    @tool(parse_docstring=True)
    async def shell_write_to_process(
        self,
        id: str,
        input: str,
        press_enter: bool
    ) -> ToolResult:
        """Write input to a running process in a specified shell session. Use for responding to interactive command prompts.
        
        Args:
            id: Unique identifier of the target shell session
            input: Input content to write to the process
            press_enter: Whether to press Enter key after input
        """
        return await self.sandbox.write_to_process(id, input, press_enter)
    
    @tool(parse_docstring=True)
    async def shell_kill_process(self, id: str) -> ToolResult:
        """Terminate a running process in a specified shell session. Use for stopping long-running processes or handling frozen commands.
        
        Args:
            id: Unique identifier of the target shell session
        """
        return await self.sandbox.kill_process(id)
