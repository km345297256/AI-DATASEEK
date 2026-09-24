import asyncio
import json
import logging
import shlex
import httpx
from typing import Any, Awaitable, Callable, ClassVar, Optional
from app.domain.external.sandbox import Sandbox
from app.domain.services.tools.base import BaseToolkit
from langchain.tools import tool
from app.domain.models.tool_result import ToolResult


logger = logging.getLogger(__name__)

_MAX_BOUNDED_TIMEOUT_SECONDS = 120
_OUTER_DEADLINE_RESERVE_SECONDS = 0.5
_SANDBOX_WAIT_TRANSPORT_GRACE_SECONDS = 1
_PROCESS_CANCELLATION_TIMEOUT_SECONDS = 5
_PROGRAM_LAUNCH_TIMEOUT_SECONDS = 30
_PROGRAM_OBSERVATION_GRACE_SECONDS = 5
_PROGRAM_RETRY_DELAY_SECONDS = 1

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
        dispose_cancellation, _ = self._process_cancellation(id)
        result = await self.sandbox.exec_command(id, exec_dir, command)
        dispose_cancellation()
        return result

    @tool(parse_docstring=True)
    async def shell_run(
        self,
        id: str,
        exec_dir: str,
        command: str,
        timeout_seconds: int = 30,
    ) -> ToolResult:
        """Run a bounded shell command and wait once for its result. Use for inspection and utilities; run custom Python analysis with program_run so shell pipelines cannot mask its exit status.

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
            recover_output=False,
        )

    @tool(parse_docstring=True)
    async def program_run(
        self, id: str, exec_dir: str, script_path: str,
        argv: list[str] | None = None, timeout_seconds: int = 30,
    ) -> ToolResult:
        """Execute a saved Python .py analysis program directly and return its own exit code, exact source version and structured failure diagnostic. Use this for custom parsing, analysis and plotting. Arguments are literal strings, never shell syntax. The output display retains a bounded head and tail with an omission marker, not complete stdout; save required evidence to files. Never add head/tail shell pipelines. A successful process still requires validation of the data and deliverables.

        Args:
            id: Unique identifier of the target process session
            exec_dir: Absolute working directory inside the sandbox
            script_path: Absolute path of the saved Python .py program inside the sandbox
            argv: Optional literal command-line arguments passed to the program
            timeout_seconds: Status observation window in seconds, clamped to 1-120. A running analysis continues across windows until completion or cancellation; this is not a total runtime limit.
        """
        from app.domain.services.program_execution import program_command
        arguments = argv or []
        return await self._run_analysis_program(
            id=id, exec_dir=exec_dir, command=program_command(script_path, arguments),
            timeout_seconds=timeout_seconds,
            launch=lambda: self.sandbox.exec_program(id, exec_dir, script_path, arguments),
        )

    async def _run_analysis_program(
        self, *, id: str, exec_dir: str, command: str, timeout_seconds: int,
        launch: Callable[[], Awaitable[ToolResult]],
    ) -> ToolResult:
        """Launch once; bounded observation windows never kill useful work.

        The real adapter pins every wait/view/kill to the original private
        operation receipt. A lost response permits observation, never relaunch.
        """
        from app.domain.services.execution_evidence import current_shell_attempt

        poll_seconds = max(1, min(timeout_seconds, _MAX_BOUNDED_TIMEOUT_SECONDS))
        uses_receipts = getattr(self.sandbox, "supports_execution_receipts", False) is True
        previous = current_shell_attempt(self.sandbox, id) if uses_receipts else None
        attempt = None

        def launched_attempt():
            current = current_shell_attempt(self.sandbox, id) if uses_receipts else None
            return current if current is not previous else None

        # If cancellation arrives before a new operation is registered, do not
        # accidentally terminate an earlier process reusing this shell name.
        dispose, kill_once = self._process_cancellation(
            id, should_cancel=lambda: not uses_receipts or launched_attempt() is not None,
        )

        def unknown() -> ToolResult:
            return ToolResult(success=False, message="Program execution state could not be confirmed; it was not restarted",
                              data={"session_id": id, "command": command, "status": "unknown", "returncode": None,
                                    "error_code": "program_execution_unconfirmed"})

        async def pause() -> None:
            await asyncio.sleep(_PROGRAM_RETRY_DELAY_SECONDS)

        try:
            try:
                async with asyncio.timeout(_PROGRAM_LAUNCH_TIMEOUT_SECONDS):
                    result = await launch()
            except (TimeoutError, httpx.TransportError, ConnectionError):
                # A launch timeout is ambiguous. Only an already registered
                # private operation gives us an identity to observe safely.
                attempt = launched_attempt()
                if attempt is None:
                    return unknown()
                result = None
            attempt = launched_attempt()
            if result is not None:
                data = self._result_data(result)
                if data.get("status") != "running":
                    if data.get("status") != "completed":
                        return result if not result.success else unknown()
                    if type(data.get("returncode")) is not int:
                        return unknown()
                    if not uses_receipts:
                        return result
                    if attempt is not None and attempt.confirmed:
                        if attempt.receipt.state != "exited" or attempt.receipt.returncode != data["returncode"]:
                            return unknown()
                        return result

            while True:
                if uses_receipts:
                    if attempt is None or launched_attempt() is not attempt or attempt.observation_block_reason:
                        return unknown()
                observed_at = asyncio.get_running_loop().time()
                try:
                    async with asyncio.timeout(poll_seconds + _PROGRAM_OBSERVATION_GRACE_SECONDS):
                        waited = await self.sandbox.wait_for_process(id, poll_seconds)
                except (TimeoutError, httpx.TransportError, ConnectionError):
                    # Retrying a read cannot duplicate the running analysis.
                    await pause()
                    continue
                except Exception:
                    return unknown()
                data = self._result_data(waited)
                status = data.get("status")
                if status == "running" and data.get("returncode") is None:
                    if asyncio.get_running_loop().time() - observed_at < _PROGRAM_RETRY_DELAY_SECONDS:
                        await pause()
                    continue
                if status != "completed" or type(data.get("returncode")) is not int:
                    return unknown()
                if uses_receipts:
                    if attempt.observation_block_reason:
                        return unknown()
                    if not attempt.confirmed and not attempt.query_in_flight:
                        # The generic finalization reconciler deliberately has
                        # a small failure budget. A still-owned running program
                        # can continue safe reads after a transient outage.
                        attempt.query_in_flight = True
                        attempt.reconciliation_queries += 1
                        try:
                            async with asyncio.timeout(_PROGRAM_OBSERVATION_GRACE_SECONDS):
                                receipt = await attempt.query()
                            if receipt.success:
                                attempt.observe(receipt.data)
                        except (TimeoutError, httpx.TransportError, ConnectionError):
                            pass
                        except Exception:
                            return unknown()
                        finally:
                            attempt.query_in_flight = False
                        if attempt.observation_block_reason:
                            return unknown()
                    if not attempt.confirmed:
                        # The leader can exit before child processes finish.
                        # Keep the same job active until the whole tree is quiet.
                        await pause()
                        continue
                    if attempt.receipt.state != "exited" or attempt.receipt.returncode != data["returncode"]:
                        return unknown()
                return await self._completed_command_result(
                    id=id, command=command, returncode=data["returncode"], retry_observation=True,
                )
        except asyncio.CancelledError:
            await self._kill_after_bounded_timeout(kill_once, reason="cancelled")
            raise
        finally:
            dispose()

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
        launch: Callable[[], Awaitable[ToolResult]] | None = None,
        recover_output: bool = True,
    ) -> ToolResult:
        timeout_seconds = max(
            1,
            min(timeout_seconds, _MAX_BOUNDED_TIMEOUT_SECONDS),
        )
        dispose_cancellation, kill_once = self._process_cancellation(id)
        exec_result = await launch() if launch is not None else await self.sandbox.exec_command(id, exec_dir, command)
        exec_data = self._result_data(exec_result)
        if exec_data.get("status") != "running":
            dispose_cancellation()
            return await self._recover_contract_output(id, exec_result) if recover_output else exec_result

        # The production interceptor owns a 120 second outer deadline. Asking
        # the sandbox to wait for that exact duration lets the outer deadline
        # win just before the sandbox can report its bounded running state.
        # Reserve transport/cleanup headroom only at that shared ceiling.
        sandbox_wait_seconds = timeout_seconds
        if timeout_seconds == _MAX_BOUNDED_TIMEOUT_SECONDS:
            sandbox_wait_seconds -= _SANDBOX_WAIT_TRANSPORT_GRACE_SECONDS
        local_wait_deadline = min(
            timeout_seconds + _SANDBOX_WAIT_TRANSPORT_GRACE_SECONDS,
            _MAX_BOUNDED_TIMEOUT_SECONDS - _OUTER_DEADLINE_RESERVE_SECONDS,
        )

        try:
            async with asyncio.timeout(local_wait_deadline):
                wait_result = await self.sandbox.wait_for_process(
                    id,
                    sandbox_wait_seconds,
                )
        except TimeoutError:
            await self._kill_after_bounded_timeout(kill_once)
            dispose_cancellation()
            return self._bounded_timeout_result(
                id=id,
                command=command,
                timeout_seconds=timeout_seconds,
            )

        wait_data = self._result_data(wait_result)
        if wait_data.get("status") != "completed":
            await self._kill_after_bounded_timeout(kill_once)
            dispose_cancellation()
            return self._bounded_timeout_result(
                id=id,
                command=command,
                timeout_seconds=timeout_seconds,
            )

        result = await self._completed_command_result(id=id, command=command, returncode=wait_data.get("returncode"))
        dispose_cancellation()
        return await self._recover_contract_output(id, result) if recover_output else result

    async def _recover_contract_output(self, id: str, result: ToolResult) -> ToolResult:
        from app.domain.services.tools.shell_output import recover_shell_output, ShellOutputUnavailable
        data = self._result_data(result)
        if not data.get("output_metadata"):
            return result
        try:
            output = await recover_shell_output(self.sandbox, id, data)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return ToolResult(success=False, message="Complete command output is unavailable; the command was not repeated",
                data={"session_id": id, "status": data.get("status"), "returncode": data.get("returncode"),
                      "error_code": error.code if isinstance(error, ShellOutputUnavailable) else "shell_output_unavailable"})
        return result.model_copy(update={"data": {**data, "output": output}})

    async def _completed_command_result(
        self, *, id: str, command: str, returncode: int | None, retry_observation: bool = False,
    ) -> ToolResult:
        while True:
            try:
                async with asyncio.timeout(_PROGRAM_OBSERVATION_GRACE_SECONDS):
                    view_result = await self.sandbox.view_shell(id)
                break
            except (TimeoutError, httpx.TransportError, ConnectionError):
                if retry_observation:
                    await asyncio.sleep(_PROGRAM_RETRY_DELAY_SECONDS)
                    continue
                view_result = ToolResult(success=False, message="Command output is unavailable")
                break
            except Exception:
                # Output transport is separate from terminal process proof.
                # Never re-execute a command to recover missing output.
                view_result = ToolResult(success=False, message="Command output is unavailable")
                break
        view_data = self._result_data(view_result)
        succeeded = returncode == 0 and view_result.success
        output_unavailable = not view_result.success
        result = ToolResult(
            success=succeeded,
            message=(
                "Command completed successfully"
                if succeeded
                else "Command completed, but its output could not be retrieved"
                if returncode == 0 and output_unavailable
                else f"Command failed with return code: {returncode}"
            ),
            data={
                "session_id": id,
                "command": command,
                "status": "completed",
                "returncode": returncode,
                "output": view_data.get("output", ""),
                **({"output_metadata": view_data["output_metadata"]}
                   if isinstance(view_data.get("output_metadata"), dict) else {}),
                **({"program_execution": view_data["program_execution"]}
                   if isinstance(view_data.get("program_execution"), dict) else {}),
                **({"error_code": "shell_output_unavailable", "output_available": False}
                   if output_unavailable else {}),
            },
        )
        return result

    def _process_cancellation(
        self,
        session_id: str,
        *, should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[Callable[[], None], Callable[[str], Awaitable[None]]]:
        """Return one idempotent kill callback tied to the current invocation."""
        kill_task: asyncio.Task[Any] | None = None

        async def kill_once(_reason: str) -> None:
            nonlocal kill_task
            if should_cancel is not None and not should_cancel():
                return
            if kill_task is None:
                kill_task = asyncio.create_task(
                    self.sandbox.kill_process(session_id),
                    name="kill-shell-process",
                )
            # If the invoking task is cancelled, keep the cleanup request alive
            # so the timeout interceptor can await the same operation once.
            await asyncio.shield(kill_task)

        disposer = self.register_tool_cancellation_callback(kill_once)
        return disposer, kill_once

    @staticmethod
    async def _kill_after_bounded_timeout(
        kill_once: Callable[[str], Awaitable[None]],
        *, reason: str = "bounded_timeout",
    ) -> None:
        try:
            async with asyncio.timeout(_PROCESS_CANCELLATION_TIMEOUT_SECONDS):
                await kill_once(reason)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The command result must remain a timeout failure even when a
            # best-effort sandbox cleanup endpoint is temporarily unavailable.
            logger.warning(
                "Bounded shell cleanup failed error_type=%s",
                type(error).__name__,
            )

    @staticmethod
    def _bounded_timeout_result(
        *,
        id: str,
        command: str,
        timeout_seconds: int,
    ) -> ToolResult:
        return ToolResult(
            success=False,
            message=(
                f"Command timed out after {timeout_seconds} seconds; "
                "process termination was requested"
            ),
            data={
                "session_id": id,
                "command": command,
                "status": "timed_out",
                "returncode": None,
            },
        )

    @staticmethod
    def _result_data(result: ToolResult) -> dict[str, Any]:
        return result.data if isinstance(result.data, dict) else {}
    
    @tool(parse_docstring=True)
    async def shell_view(self, id: str, output_id: Optional[str] = None,
                         cursor: Optional[int] = None, max_bytes: int = 8192) -> ToolResult:
        """View bounded shell output or read a private log incrementally. First call with id only to get a preview and output_metadata.output_id; to recover complete text call with that output_id and cursor=0, then follow output_page.next_cursor. Each observer owns its cursor. A preview is not complete scientific evidence. Inspect log_status and output_page.lossy: missing output cannot be reconstructed by repeating the command. Logs retire when the command is replaced, the shell is released, or the sandbox restarts.
        
        Args:
            id: Unique identifier of the target shell session
            output_id: Exact opaque output generation returned by a previous view or execution
            cursor: Absolute UTF-8 byte offset, required together with output_id
            max_bytes: Maximum page bytes from 4 to 16384, used only with cursor
        """
        if output_id is None and cursor is None:
            return await self.sandbox.view_shell(id)
        return await self.sandbox.view_shell(id, output_id=output_id, cursor=cursor, max_bytes=max_bytes)
    
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
        dispose_cancellation, _ = self._process_cancellation(id)
        result = await self.sandbox.wait_for_process(id, seconds)
        dispose_cancellation()
        return result
    
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
