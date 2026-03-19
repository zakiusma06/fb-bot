import { Router, type IRouter } from "express";
import healthRouter from "./health";
import shopifyRouter from "./shopify";

const router: IRouter = Router();

router.use(healthRouter);
router.use(shopifyRouter);

export default router;
