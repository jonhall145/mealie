import base64
import json
import re
from pathlib import Path

import anthropic
from pydantic import UUID4

from mealie.core import root_logger
from mealie.core.config import get_app_settings
from mealie.lang.providers import Translator
from mealie.pkgs import img
from mealie.repos.repository_factory import AllRepositories
from mealie.schema.recipe.recipe import Recipe, create_recipe_slug
from mealie.schema.recipe.recipe_ingredient import RecipeIngredient
from mealie.schema.recipe.recipe_step import RecipeStep
from mealie.schema.recipe.recipe_notes import RecipeNote
from mealie.schema.reports.reports import (
    ReportCategory,
    ReportCreate,
    ReportEntryCreate,
    ReportEntryOut,
    ReportSummaryStatus,
)
from mealie.schema.user.user import GroupInDB, PrivateUser
from mealie.services._base_service import BaseService
from mealie.services.recipe.recipe_service import RecipeService

logger = root_logger.get_logger(__name__)

SYSTEM_PROMPT = (
    "You are a bot that reads an image and parses it into recipe JSON. "
    "You will receive an image from the user and you need to extract the recipe data. "
    "It is imperative that you do not create any data or otherwise make up any information.\n\n"
    "The user message that you receive will be one image containing a single recipe. "
    "The recipe may consist of printed text or handwritten text. "
    "It may be rotated or not properly cropped. "
    "It is your job to figure out which part of the image is the important content and extract it.\n\n"
    "Respond ONLY with valid JSON matching the provided schema. No other text."
)

RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Recipe name or title"},
        "description": {"type": ["string", "null"], "description": "Brief description"},
        "recipe_yield": {"type": ["string", "null"], "description": "e.g. '4 servings'"},
        "total_time": {"type": ["string", "null"], "description": "e.g. '1 hour 30 minutes'"},
        "prep_time": {"type": ["string", "null"], "description": "e.g. '30 minutes'"},
        "perform_time": {"type": ["string", "null"], "description": "e.g. '1 hour'"},
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
        "instructions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    "required": ["name"],
}


def _image_to_base64(image_path: Path) -> str:
    """Convert an image to JPEG and return base64-encoded data."""
    jpg_path = img.PillowMinifier.to_jpg(
        image_path, dest=image_path.parent / f"{image_path.stem}-min.jpg"
    )
    with open(jpg_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _convert_recipe(data: dict, user: PrivateUser, household_id: UUID4) -> Recipe:
    """Convert parsed JSON dict to a Recipe model."""
    return Recipe(
        user_id=user.id,
        group_id=user.group_id,
        household_id=household_id,
        name=data.get("name", "Untitled Recipe"),
        slug=create_recipe_slug(data.get("name", "Untitled Recipe")),
        description=data.get("description"),
        recipe_yield=data.get("recipe_yield"),
        total_time=data.get("total_time"),
        prep_time=data.get("prep_time"),
        perform_time=data.get("perform_time"),
        recipe_ingredient=[
            RecipeIngredient(title=None, note=ing) if isinstance(ing, str)
            else RecipeIngredient(title=ing.get("title"), note=ing.get("text", ""))
            for ing in data.get("ingredients", [])
            if (ing if isinstance(ing, str) else ing.get("text"))
        ],
        recipe_instructions=[
            RecipeStep(title=None, text=ins) if isinstance(ins, str)
            else RecipeStep(title=ins.get("title"), text=ins.get("text", ""))
            for ins in data.get("instructions", [])
            if (ins if isinstance(ins, str) else ins.get("text"))
        ],
        notes=[
            RecipeNote(title="", text=note) if isinstance(note, str)
            else RecipeNote(title=note.get("title") or "", text=note.get("text", ""))
            for note in data.get("notes", [])
            if (note if isinstance(note, str) else note.get("text"))
        ],
    )


class RecipeBatchService(BaseService):
    """Handles bulk recipe creation from images using Claude Batch API."""

    def __init__(
        self,
        service: RecipeService,
        repos: AllRepositories,
        group: GroupInDB,
        user: PrivateUser,
        household_id: UUID4,
        translator: Translator,
    ) -> None:
        self.service = service
        self.repos = repos
        self.group = group
        self.user = user
        self.household_id = household_id
        self.translator = translator
        self.report_entries: list[ReportEntryCreate] = []
        super().__init__()

    def _get_client(self) -> anthropic.Anthropic:
        settings = get_app_settings()
        return anthropic.Anthropic(api_key=settings.OPENAI_API_KEY)

    def get_report_id(self) -> UUID4:
        import_report = ReportCreate(
            name="Batch Image Import",
            category=ReportCategory.bulk_import,
            status=ReportSummaryStatus.in_progress,
            group_id=self.group.id,
        )
        self.report = self.repos.group_reports.create(import_report)
        return self.report.id

    def _save_all_entries(self) -> None:
        is_success = True
        is_failure = True

        new_entries: list[ReportEntryOut] = []
        for entry in self.report_entries:
            if is_failure and entry.success:
                is_failure = False
            if is_success and not entry.success:
                is_success = False
            new_entries.append(self.repos.group_report_entries.create(entry))

        if is_success:
            self.report.status = ReportSummaryStatus.success
        if is_failure:
            self.report.status = ReportSummaryStatus.failure
        if not is_success and not is_failure:
            self.report.status = ReportSummaryStatus.partial

        self.report.entries = new_entries
        self.repos.group_reports.update(self.report.id, self.report)

    @staticmethod
    def _sanitize_custom_id(filename: str, index: int) -> str:
        """Create a batch-API-safe custom_id from a filename."""
        # Strip extension, replace non-alphanumeric with underscore, truncate
        stem = Path(filename).stem
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", stem)[:50]
        return f"{index:03d}_{safe}" if safe else f"{index:03d}_image"

    def create_batch(self, image_dir: Path, image_files: list[str]) -> tuple[str, dict[str, str]]:
        """
        Submit a Claude Message Batch for all images.
        Returns (batch_id, id_to_filename mapping).
        """
        client = self._get_client()

        batch_requests = []
        id_to_filename: dict[str, str] = {}

        for i, filename in enumerate(image_files):
            image_path = image_dir / filename
            if not image_path.exists():
                logger.warning(f"Image file not found: {image_path}")
                continue

            b64_data = _image_to_base64(image_path)
            custom_id = self._sanitize_custom_id(filename, i)
            id_to_filename[custom_id] = filename

            batch_requests.append(
                anthropic.types.messages.batch_create_params.Request(
                    custom_id=custom_id,
                    params=anthropic.types.message_create_params.MessageCreateParamsNonStreaming(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=4096,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/jpeg",
                                            "data": b64_data,
                                        },
                                    },
                                    {
                                        "type": "text",
                                        "text": "Please extract the recipe from this image. Respond with JSON only.",
                                    },
                                ],
                            }
                        ],
                        system=SYSTEM_PROMPT,
                    ),
                )
            )

        if not batch_requests:
            raise ValueError("No valid images to process")

        batch = client.messages.batches.create(requests=batch_requests)
        return batch.id, id_to_filename

    def check_batch(self, batch_id: str) -> dict:
        """Check batch status. Returns status info."""
        client = self._get_client()
        batch = client.messages.batches.retrieve(batch_id)
        return {
            "id": batch.id,
            "processing_status": batch.processing_status,
            "request_counts": {
                "processing": batch.request_counts.processing,
                "succeeded": batch.request_counts.succeeded,
                "errored": batch.request_counts.errored,
                "canceled": batch.request_counts.canceled,
                "expired": batch.request_counts.expired,
            },
        }

    def collect_results(self, batch_id: str, image_dir: Path, id_to_filename: dict[str, str]) -> list[str]:
        """
        Collect batch results, create recipes, and save images.
        Returns list of created recipe slugs.
        """
        from mealie.services.scraper import cleaner

        client = self._get_client()
        created_slugs: list[str] = []

        for result in client.messages.batches.results(batch_id):
            custom_id = result.custom_id
            filename = id_to_filename.get(custom_id, custom_id)
            try:
                if result.result.type != "succeeded":
                    error_msg = f"Batch request failed for {filename}: {result.result.type}"
                    logger.error(error_msg)
                    self.report_entries.append(
                        ReportEntryCreate(
                            report_id=self.report.id,
                            success=False,
                            message=f"Failed to process {filename}",
                            exception=error_msg,
                        )
                    )
                    continue

                # Extract text content from response
                message = result.result.message
                text_content = ""
                for block in message.content:
                    if block.type == "text":
                        text_content += block.text

                # Parse JSON from response
                # Handle potential markdown code fences
                text_content = text_content.strip()
                if text_content.startswith("```"):
                    lines = text_content.split("\n")
                    # Remove first and last lines (``` markers)
                    lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                    text_content = "\n".join(lines)

                recipe_data = json.loads(text_content)
                recipe = _convert_recipe(recipe_data, self.user, self.household_id)
                recipe = cleaner.clean(recipe, self.translator)

                created_recipe = self.service.create_one(recipe)

                # Save the original image as the recipe image
                image_path = image_dir / filename
                if image_path.exists():
                    from mealie.services.recipe.recipe_data_service import RecipeDataService

                    data_service = RecipeDataService(created_recipe.id)
                    with open(image_path, "rb") as f:
                        data_service.write_image(f.read(), "webp")

                created_slugs.append(created_recipe.slug)
                self.report_entries.append(
                    ReportEntryCreate(
                        report_id=self.report.id,
                        success=True,
                        message=f"Successfully imported recipe: {created_recipe.name}",
                    )
                )

            except Exception as e:
                logger.error(f"Failed to process result for {filename}: {e}")
                logger.exception(e)
                self.report_entries.append(
                    ReportEntryCreate(
                        report_id=self.report.id,
                        success=False,
                        message=f"Failed to process {filename}",
                        exception=str(e),
                    )
                )

        self._save_all_entries()
        return created_slugs
